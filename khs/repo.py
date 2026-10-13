"""Content-addressed snapshot repository on a local disk (for example an external USB drive).

Layout::

    REPO/
      config.json            format version and chunk size
      lock                   held (flock) while a command changes the repository
      objects/ab/abcdef...   file chunks, named by the SHA-256 of their content
      snapshots/<id>.json    one manifest per snapshot, written last

Every write goes to a temporary file in the same directory and is moved into place, so an
interrupted backup never leaves a half-written object or a snapshot pointing at missing data.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

FORMAT = 1
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024

_RAW = b"R"  # object stored as-is
_ZLIB = b"Z"  # object stored zlib-compressed


class RepoError(Exception):
    """A problem the user can act on (wrong path, repository in use, unknown snapshot...)."""


class CorruptObject(RepoError):
    def __init__(self, digest: str, reason: str):
        super().__init__(f"object {digest[:12]} is corrupt: {reason}")
        self.digest = digest


@dataclass
class Snapshot:
    id: str
    time: dt.datetime
    hostname: str
    sources: list[str]
    entries: list[dict[str, Any]]
    tags: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def chunks(self) -> Iterator[str]:
        for e in self.entries:
            yield from e.get("chunks", ())

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "id": self.id,
            "time": self.time.isoformat(),
            "hostname": self.hostname,
            "sources": self.sources,
            "tags": self.tags,
            "stats": self.stats,
            "entries": self.entries,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Snapshot":
        return cls(
            id=d["id"],
            time=dt.datetime.fromisoformat(d["time"]),
            hostname=d.get("hostname", ""),
            sources=list(d.get("sources", [])),
            entries=list(d.get("entries", [])),
            tags=list(d.get("tags", [])),
            stats=dict(d.get("stats", {})),
        )


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class Repository:
    def __init__(self, path: Path, chunk_size: int):
        self.path = path
        self.chunk_size = chunk_size

    # ------------------------------------------------------------------ setup
    @classmethod
    def init(cls, path: str | os.PathLike[str], chunk_size: int = DEFAULT_CHUNK_SIZE) -> "Repository":
        p = Path(path)
        if p.exists() and any(p.iterdir()):
            raise RepoError(f"{p} exists and is not empty")
        if not 64 * 1024 <= chunk_size <= 64 * 1024 * 1024:
            raise RepoError("chunk size must be between 64 KiB and 64 MiB")
        (p / "objects").mkdir(parents=True, exist_ok=True)
        (p / "snapshots").mkdir(exist_ok=True)
        os.chmod(p, 0o700)
        cfg = {"format": FORMAT, "chunk_size": chunk_size, "created": dt.datetime.now(dt.timezone.utc).isoformat()}
        _atomic_write(p / "config.json", json.dumps(cfg, indent=2).encode())
        return cls(p, chunk_size)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "Repository":
        p = Path(path)
        try:
            cfg = json.loads((p / "config.json").read_text())
        except FileNotFoundError:
            raise RepoError(f"{p} is not a khs repository (run `khs init {p}` first)") from None
        except (OSError, ValueError) as e:
            raise RepoError(f"cannot read {p / 'config.json'}: {e}") from None
        if cfg.get("format") != FORMAT:
            raise RepoError(f"unsupported repository format {cfg.get('format')!r}")
        return cls(p, int(cfg["chunk_size"]))

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive lock so a backup and a prune can never run on the same repository at once."""
        with open(self.path / "lock", "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RepoError("another khs command is using this repository") from None
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # --------------------------------------------------------------- objects
    def _object_path(self, digest: str) -> Path:
        return self.path / "objects" / digest[:2] / digest

    def has_object(self, digest: str) -> bool:
        return self._object_path(digest).exists()

    def put_object(self, data: bytes) -> tuple[str, int]:
        """Store a chunk; returns (digest, bytes written). Existing chunks cost nothing."""
        digest = hashlib.sha256(data).hexdigest()
        path = self._object_path(digest)
        if path.exists():
            return digest, 0
        packed = zlib.compress(data, 6)
        payload = _ZLIB + packed if len(packed) < len(data) else _RAW + data
        path.parent.mkdir(exist_ok=True)
        _atomic_write(path, payload)
        return digest, len(payload)

    def get_object(self, digest: str) -> bytes:
        try:
            payload = self._object_path(digest).read_bytes()
        except FileNotFoundError:
            raise CorruptObject(digest, "missing") from None
        kind, body = payload[:1], payload[1:]
        try:
            data = zlib.decompress(body) if kind == _ZLIB else body if kind == _RAW else None
        except zlib.error as e:
            raise CorruptObject(digest, f"cannot decompress ({e})") from None
        if data is None:
            raise CorruptObject(digest, "unknown encoding")
        if hashlib.sha256(data).hexdigest() != digest:
            raise CorruptObject(digest, "content does not match its hash")
        return data

    def object_digests(self) -> Iterator[str]:
        for sub in sorted((self.path / "objects").iterdir()):
            if sub.is_dir():
                for f in sorted(sub.iterdir()):
                    if not f.name.startswith(".tmp-"):
                        yield f.name

    def delete_object(self, digest: str) -> int:
        p = self._object_path(digest)
        size = p.stat().st_size
        p.unlink()
        return size

    def remove_temp_files(self) -> int:
        """Leftovers of an interrupted write; only safe to call while holding the lock."""
        n = 0
        for tmp in list(self.path.glob("objects/*/.tmp-*")) + list(self.path.glob("snapshots/.tmp-*")):
            tmp.unlink(missing_ok=True)
            n += 1
        return n

    # ------------------------------------------------------------- snapshots
    def save_snapshot(self, snap: Snapshot) -> None:
        _atomic_write(self.path / "snapshots" / f"{snap.id}.json", json.dumps(snap.to_json(), separators=(",", ":")).encode())

    @staticmethod
    def new_snapshot_id(when: dt.datetime) -> str:
        return f"{when.astimezone(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"

    def snapshots(self) -> list[Snapshot]:
        out = []
        for f in (self.path / "snapshots").glob("*.json"):
            try:
                out.append(Snapshot.from_json(json.loads(f.read_text())))
            except (OSError, ValueError, KeyError) as e:
                raise RepoError(f"snapshot file {f.name} is unreadable: {e}") from None
        return sorted(out, key=lambda s: (s.time, s.id))

    def find_snapshot(self, ref: str) -> Snapshot:
        """`latest`, a full id, or a unique id prefix."""
        snaps = self.snapshots()
        if not snaps:
            raise RepoError("the repository has no snapshots yet")
        if ref == "latest":
            return snaps[-1]
        matches = [s for s in snaps if s.id == ref] or [s for s in snaps if s.id.startswith(ref)]
        if len(matches) != 1:
            raise RepoError(f"no snapshot matches {ref!r}" if not matches else f"{ref!r} matches {len(matches)} snapshots")
        return matches[0]

    def delete_snapshot(self, snap_id: str) -> None:
        (self.path / "snapshots" / f"{snap_id}.json").unlink()
