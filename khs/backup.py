"""Backup, restore and integrity check on top of :mod:`khs.repo`."""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .repo import CorruptObject, RepoError, Repository, Snapshot

Progress = Callable[[str], None]


@dataclass
class BackupResult:
    snapshot: Snapshot
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    unchanged: int = 0
    bytes_read: int = 0
    bytes_stored: int = 0
    skipped: list[str] = field(default_factory=list)  # human-readable reasons
    changed_during_backup: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.skipped or self.changed_during_backup)


def _entry_key(abs_path: str) -> str:
    """Absolute path stored without the leading slash, so restores land under the target directory."""
    return abs_path.lstrip("/")


def _excluded(rel: str, name: str, patterns: Iterable[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase("/" + rel, pat):
            return True
    return False


def _open_for_backup(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    if noatime:
        # O_NOATIME needs file ownership or CAP_FOWNER; the hardened systemd unit grants only
        # CAP_DAC_READ_SEARCH, so other users' files fall back to a normal open.
        try:
            return os.open(path, flags | noatime)
        except PermissionError:
            pass
    return os.open(path, flags)


def _read_chunks(path: str, chunk_size: int) -> Iterable[bytes]:
    fd = _open_for_backup(path)
    try:
        with os.fdopen(fd, "rb", closefd=False) as f:
            while chunk := f.read(chunk_size):
                yield chunk
    finally:
        os.close(fd)


def backup(
    repo: Repository,
    sources: list[str],
    excludes: Iterable[str] = (),
    tags: Iterable[str] = (),
    now: dt.datetime | None = None,
    hostname: str | None = None,
    progress: Progress | None = None,
) -> BackupResult:
    """Snapshot the given directories or files. Unchanged files reuse the previous snapshot's chunks."""
    if not sources:
        raise RepoError("nothing to back up: no sources given")
    srcs = []
    for s in sources:
        p = os.path.abspath(s)
        if not os.path.lexists(p):
            raise RepoError(f"source does not exist: {p}")
        srcs.append(p)
    repo_abs = os.path.realpath(repo.path)
    excludes = list(excludes)
    when = now or dt.datetime.now(dt.timezone.utc)

    previous: dict[str, dict[str, Any]] = {}
    for snap in reversed(repo.snapshots()):
        if sorted(snap.sources) == sorted(srcs):
            previous = {e["path"]: e for e in snap.entries if e["type"] == "file"}
            break

    snap = Snapshot(
        id=Repository.new_snapshot_id(when),
        time=when,
        hostname=hostname or socket.gethostname(),
        sources=srcs,
        entries=[],
        tags=sorted(set(tags)),
    )
    res = BackupResult(snapshot=snap)

    def add(abs_path: str, st: os.stat_result) -> None:
        key = _entry_key(abs_path)
        base = {"path": key, "mode": stat.S_IMODE(st.st_mode), "mtime_ns": st.st_mtime_ns, "uid": st.st_uid, "gid": st.st_gid}
        if stat.S_ISDIR(st.st_mode):
            snap.entries.append({**base, "type": "dir"})
            res.dirs += 1
        elif stat.S_ISLNK(st.st_mode):
            snap.entries.append({**base, "type": "symlink", "target": os.readlink(abs_path)})
            res.symlinks += 1
        elif stat.S_ISREG(st.st_mode):
            ident = {"size": st.st_size, "inode": st.st_ino, "dev": st.st_dev, "ctime_ns": st.st_ctime_ns}
            prev = previous.get(key)
            if (
                prev
                and all(prev.get(k) == v for k, v in ident.items())
                and prev["mtime_ns"] == st.st_mtime_ns
                and all(repo.has_object(c) for c in prev["chunks"])
            ):
                snap.entries.append({**base, "type": "file", **ident, "chunks": prev["chunks"]})
                res.files += 1
                res.unchanged += 1
                return
            chunks = []
            try:
                for data in _read_chunks(abs_path, repo.chunk_size):
                    digest, stored = repo.put_object(data)
                    chunks.append(digest)
                    res.bytes_read += len(data)
                    res.bytes_stored += stored
            except OSError as e:
                res.skipped.append(f"{abs_path}: {e.strerror or e}")
                return
            after = os.lstat(abs_path)
            if after.st_mtime_ns != st.st_mtime_ns or after.st_size != st.st_size:
                res.changed_during_backup.append(abs_path)
            snap.entries.append({**base, "type": "file", **ident, "chunks": chunks})
            res.files += 1
        else:
            res.skipped.append(f"{abs_path}: special file (socket, fifo or device) not backed up")

    recorded_parents: set[str] = set()
    for src in srcs:
        # Parents of the source are recorded so a restore recreates them with their permissions.
        parts = PurePosixPath(src).parts
        for i in range(2, len(parts)):
            parent = str(PurePosixPath(*parts[:i]))
            if parent not in recorded_parents and parent not in srcs:
                recorded_parents.add(parent)
                add(parent, os.lstat(parent))
        st = os.lstat(src)
        add(src, st)
        if not stat.S_ISDIR(st.st_mode):
            continue
        for root, dirs, files in os.walk(src, onerror=lambda e: res.skipped.append(f"{e.filename}: {e.strerror}")):
            dirs.sort()
            kept = []
            for d in dirs:
                full = os.path.join(root, d)
                rel = os.path.relpath(full, src)
                if os.path.realpath(full) == repo_abs or _excluded(rel, d, excludes):
                    continue
                try:
                    st = os.lstat(full)
                except OSError as e:
                    res.skipped.append(f"{full}: {e.strerror}")
                    continue
                add(full, st)
                if stat.S_ISDIR(st.st_mode):
                    kept.append(d)
            dirs[:] = kept
            for name in sorted(files):
                full = os.path.join(root, name)
                if _excluded(os.path.relpath(full, src), name, excludes):
                    continue
                try:
                    st = os.lstat(full)
                except OSError as e:
                    res.skipped.append(f"{full}: {e.strerror}")
                    continue
                add(full, st)
                if progress and res.files % 500 == 0:
                    progress(f"{res.files} files")

    snap.stats = {
        "files": res.files,
        "dirs": res.dirs,
        "symlinks": res.symlinks,
        "unchanged": res.unchanged,
        "bytes_read": res.bytes_read,
        "bytes_stored": res.bytes_stored,
        "total_size": sum(e.get("size", 0) for e in snap.entries),
        "skipped": len(res.skipped),
        "changed_during_backup": len(res.changed_during_backup),
    }
    repo.save_snapshot(snap)  # last: until this file exists, the snapshot doesn't
    return res


# ---------------------------------------------------------------- restore


@dataclass
class RestoreResult:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    bytes_written: int = 0
    skipped_existing: list[str] = field(default_factory=list)


def _safe_destination(target: Path, key: str) -> Path:
    rel = PurePosixPath(key)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise RepoError(f"refusing to restore unsafe path {key!r}")
    return target.joinpath(*rel.parts)


def _make_dirs(target: Path, path: Path) -> None:
    """Create ``path`` below ``target`` one component at a time, refusing to pass through a symlink.

    Checking after ``mkdir(parents=True)`` would be too late: a symlink already present in the
    target (say ``srv -> /etc``) would have been followed.
    """
    cur = target
    for part in path.relative_to(target).parts:
        cur = cur / part
        if cur.is_symlink():
            raise RepoError(f"refusing to restore through symlink {cur}")
        if cur.exists():
            if not cur.is_dir():
                raise RepoError(f"refusing to restore into {cur}: not a directory")
        else:
            cur.mkdir()


def restore(
    repo: Repository,
    snap: Snapshot,
    target: str | os.PathLike[str],
    include: str | None = None,
    overwrite: bool = False,
    owners: bool | None = None,
) -> RestoreResult:
    """Restore a snapshot under ``target``. Existing files are kept unless ``overwrite`` is set.

    Paths are checked so a manifest can't make the restore write outside the target, and symlinks
    are created last so files are never written through a restored link. ``owners`` restores the
    original uid/gid; by default it does so when running as root (services such as Syncthing break
    if their data comes back owned by root).
    """
    if owners is None:
        owners = os.geteuid() == 0

    def chown(path: Path, e: dict, follow: bool = True) -> None:
        if owners and "uid" in e:
            os.chown(path, e["uid"], e["gid"], follow_symlinks=follow)
    tgt = Path(os.path.realpath(target))
    tgt.mkdir(parents=True, exist_ok=True)
    inc = include.strip("/") if include else None

    def wanted(key: str) -> bool:
        return inc is None or key == inc or key.startswith(inc + "/") or inc.startswith(key + "/")

    entries = [e for e in snap.entries if wanted(e["path"])]
    if inc and not entries:
        raise RepoError(f"nothing in snapshot {snap.id} matches {include!r}")
    res = RestoreResult()

    for e in entries:
        if e["type"] != "dir":
            continue
        _make_dirs(tgt, _safe_destination(tgt, e["path"]))
        res.dirs += 1

    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    for e in entries:
        if e["type"] != "file":
            continue
        dest = _safe_destination(tgt, e["path"])
        _make_dirs(tgt, dest.parent)
        if dest.exists() or dest.is_symlink():
            if not overwrite:
                res.skipped_existing.append(str(dest))
                continue
            if dest.is_symlink() or not dest.is_file():
                raise RepoError(f"refusing to overwrite {dest}: not a regular file")
        fd = os.open(dest, flags | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            for digest in e["chunks"]:
                data = repo.get_object(digest)
                f.write(data)
                res.bytes_written += len(data)
        chown(dest, e)
        os.chmod(dest, e["mode"])  # after chown, which clears setuid/setgid bits
        os.utime(dest, ns=(e["mtime_ns"], e["mtime_ns"]))
        res.files += 1

    for e in entries:
        if e["type"] != "symlink":
            continue
        dest = _safe_destination(tgt, e["path"])
        _make_dirs(tgt, dest.parent)
        if dest.exists() or dest.is_symlink():
            if not overwrite:
                res.skipped_existing.append(str(dest))
                continue
            if dest.is_dir() and not dest.is_symlink():
                raise RepoError(f"refusing to replace directory {dest} with a symlink")
            dest.unlink()
        os.symlink(e["target"], dest)
        chown(dest, e, follow=False)
        res.symlinks += 1

    # Directory times last, deepest first, because writing children changes them.
    for e in sorted((e for e in entries if e["type"] == "dir"), key=lambda e: e["path"].count("/"), reverse=True):
        d = _safe_destination(tgt, e["path"])
        chown(d, e)
        os.chmod(d, e["mode"])
        os.utime(d, ns=(e["mtime_ns"], e["mtime_ns"]))
    return res


# ------------------------------------------------------------------ check


@dataclass
class CheckResult:
    snapshots: int = 0
    objects_checked: int = 0
    missing: dict[str, list[str]] = field(default_factory=dict)  # digest -> snapshot ids
    corrupt: dict[str, list[str]] = field(default_factory=dict)
    unreferenced: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing and not self.corrupt

    @property
    def damaged_snapshots(self) -> list[str]:
        return sorted({s for ids in (*self.missing.values(), *self.corrupt.values()) for s in ids})


def check(repo: Repository, read_data: bool = False) -> CheckResult:
    """Every chunk referenced by a snapshot must exist; with ``read_data`` each one is also re-hashed."""
    res = CheckResult()
    used: dict[str, list[str]] = {}
    for snap in repo.snapshots():
        res.snapshots += 1
        for digest in set(snap.chunks()):
            used.setdefault(digest, []).append(snap.id)
    for digest, ids in used.items():
        if not repo.has_object(digest):
            res.missing[digest] = ids
            continue
        if read_data:
            try:
                repo.get_object(digest)
            except CorruptObject:
                res.corrupt[digest] = ids
        res.objects_checked += 1
    res.unreferenced = sum(1 for d in repo.object_digests() if d not in used)
    return res
