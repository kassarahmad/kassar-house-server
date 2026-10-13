import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khs.backup import backup, check, restore
from khs.repo import CorruptObject, RepoError, Repository

SMALL = 64 * 1024  # smallest allowed chunk size, so multi-chunk files stay small in tests


def tree(root: Path) -> dict[str, tuple]:
    """Everything that matters about a directory tree: type, content hash, mode, mtime, link target."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath, name)
            st = os.lstat(p)
            rel = str(p.relative_to(root))
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode), st.st_mtime_ns)
            else:
                out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(st.st_mode), st.st_mtime_ns)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self.tmp / "data"
        self.src.mkdir()
        self.repo = Repository.init(self.tmp / "repo", chunk_size=SMALL)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel: str, data: bytes | str, mode: int = 0o644) -> Path:
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        os.chmod(p, mode)
        return p

    def restored(self, out: Path) -> Path:
        return out / str(self.src).lstrip("/")


class RoundTrip(Base):
    def test_restore_reproduces_content_modes_times_and_symlinks(self):
        self.write("notes.txt", "ciao")
        self.write("empty", b"")
        self.write("bin/run.sh", "#!/bin/sh\necho hi\n", 0o755)
        self.write("big/video.bin", os.urandom(3 * SMALL + 123))
        self.write("città/è un file con spazi.txt", "unicode")
        os.symlink("notes.txt", self.src / "link-to-notes")
        os.symlink("/etc/hostname", self.src / "absolute-link")
        (self.src / "empty-dir").mkdir()
        os.utime(self.src / "notes.txt", ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))

        res = backup(self.repo, [str(self.src)])
        self.assertFalse(res.partial)
        self.assertEqual(res.files, 5)
        out = self.tmp / "out"
        r = restore(self.repo, self.repo.find_snapshot("latest"), out)
        self.assertEqual(r.files, 5)
        self.assertEqual(tree(self.src), tree(self.restored(out)))

    @unittest.skipUnless(os.geteuid() == 0, "changing file owners needs root")
    def test_owners_are_restored_when_running_as_root(self):
        f = self.write("svc/data.db", "x")
        os.chown(f, 1234, 2345)
        os.chown(f.parent, 1234, 2345)
        os.symlink("data.db", f.parent / "current")
        os.chown(f.parent / "current", 1234, 2345, follow_symlinks=False)
        os.chmod(f, 0o4755)  # setuid survives because chmod runs after chown
        backup(self.repo, [str(self.src)])
        out = self.tmp / "out"
        restore(self.repo, self.repo.find_snapshot("latest"), out)
        r = self.restored(out) / "svc"
        self.assertEqual((os.stat(r / "data.db").st_uid, os.stat(r / "data.db").st_gid), (1234, 2345))
        self.assertEqual(stat.S_IMODE(os.stat(r / "data.db").st_mode), 0o4755)
        self.assertEqual(os.lstat(r / "current").st_uid, 1234)
        self.assertEqual(os.stat(r).st_uid, 1234)
        out2 = self.tmp / "out2"
        restore(self.repo, self.repo.find_snapshot("latest"), out2, owners=False)
        self.assertEqual(os.stat(self.restored(out2) / "svc" / "data.db").st_uid, 0)

    def test_include_restores_only_one_subtree(self):
        self.write("a/keep.txt", "1")
        self.write("b/other.txt", "2")
        backup(self.repo, [str(self.src)])
        out = self.tmp / "out"
        key = str(self.src / "a").lstrip("/")
        restore(self.repo, self.repo.find_snapshot("latest"), out, include="/" + key)
        self.assertTrue((out / key / "keep.txt").exists())
        self.assertFalse((self.restored(out) / "b").exists())
        with self.assertRaises(RepoError):
            restore(self.repo, self.repo.find_snapshot("latest"), out, include="nope")

    def test_existing_files_are_kept_unless_overwrite(self):
        self.write("f.txt", "from backup")
        backup(self.repo, [str(self.src)])
        out = self.tmp / "out"
        dest = self.restored(out) / "f.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("local change")
        r = restore(self.repo, self.repo.find_snapshot("latest"), out)
        self.assertEqual(dest.read_text(), "local change")
        self.assertEqual(r.skipped_existing, [str(dest)])
        restore(self.repo, self.repo.find_snapshot("latest"), out, overwrite=True)
        self.assertEqual(dest.read_text(), "from backup")


class Deduplication(Base):
    def test_second_backup_of_unchanged_data_reads_and_stores_nothing(self):
        self.write("a.bin", os.urandom(2 * SMALL))
        first = backup(self.repo, [str(self.src)])
        second = backup(self.repo, [str(self.src)])
        self.assertGreater(first.bytes_stored, 0)
        self.assertEqual((second.bytes_read, second.bytes_stored, second.unchanged), (0, 0, 1))
        self.assertEqual(len(self.repo.snapshots()), 2)

    def test_identical_content_is_stored_once(self):
        blob = os.urandom(SMALL)
        self.write("one.bin", blob)
        self.write("copy/two.bin", blob)
        res = backup(self.repo, [str(self.src)])
        self.assertEqual(len(list(self.repo.object_digests())), 1)
        self.assertLess(res.bytes_stored, 2 * SMALL)

    def test_changing_one_chunk_stores_only_that_chunk(self):
        data = bytearray(os.urandom(4 * SMALL))
        p = self.write("disk.img", bytes(data))
        backup(self.repo, [str(self.src)])
        before = set(self.repo.object_digests())
        data[2 * SMALL + 10] ^= 0xFF
        p.write_bytes(bytes(data))
        res = backup(self.repo, [str(self.src)])
        self.assertEqual(len(set(self.repo.object_digests()) - before), 1)
        self.assertEqual(res.bytes_read, 4 * SMALL)  # changed file is re-read in full

    def test_edit_that_keeps_size_and_mtime_is_still_detected(self):
        # Some tools rewrite a file and put the old mtime back (rsync -t, cp -p, editors).
        p = self.write("config.yml", "port: 8080\n")
        before = os.stat(p)
        backup(self.repo, [str(self.src)])
        p.write_text("port: 9090\n")
        os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
        snap = backup(self.repo, [str(self.src)]).snapshot
        out = self.tmp / "out"
        restore(self.repo, snap, out)
        self.assertEqual((self.restored(out) / "config.yml").read_text(), "port: 9090\n")

    def test_compressible_data_is_compressed(self):
        self.write("log.txt", "same line\n" * 20000)
        res = backup(self.repo, [str(self.src)])
        self.assertLess(res.bytes_stored, res.bytes_read / 10)


class Selection(Base):
    def test_excludes_match_names_and_paths(self):
        self.write("keep.txt", "k")
        self.write("x.tmp", "t")
        self.write("cache/big.bin", "c")
        self.write("deep/node_modules/pkg/index.js", "n")
        res = backup(self.repo, [str(self.src)], excludes=["*.tmp", "cache", "node_modules"])
        paths = {e["path"].rsplit("/", 1)[-1] for e in res.snapshot.entries}
        self.assertIn("keep.txt", paths)
        self.assertTrue({"x.tmp", "cache", "big.bin", "node_modules", "index.js"}.isdisjoint(paths))

    def test_repository_inside_a_source_is_not_backed_up(self):
        repo = Repository.init(self.src / "backups", chunk_size=SMALL)
        self.write("a.txt", "a")
        res = backup(repo, [str(self.src)])
        self.assertFalse(any("backups" in e["path"] for e in res.snapshot.entries))

    def test_special_files_are_skipped_and_reported(self):
        self.write("a.txt", "a")
        os.mkfifo(self.src / "pipe")
        res = backup(self.repo, [str(self.src)])
        self.assertTrue(res.partial)
        self.assertIn("special file", res.skipped[0])
        self.assertEqual(res.snapshot.stats["skipped"], 1)

    def test_missing_source_is_an_error(self):
        with self.assertRaises(RepoError):
            backup(self.repo, [str(self.tmp / "nope")])


class Safety(Base):
    def test_interrupted_backup_leaves_no_snapshot_and_prunable_chunks(self):
        from khs.retention import gc

        self.write("a.bin", os.urandom(3 * SMALL))
        real_put = self.repo.put_object
        calls = []

        def failing_put(data):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_put(data)

        with mock.patch.object(self.repo, "put_object", side_effect=failing_put):
            with self.assertRaises(KeyboardInterrupt):
                backup(self.repo, [str(self.src)])
        self.assertEqual(self.repo.snapshots(), [])
        self.assertEqual(len(list(self.repo.object_digests())), 1)
        self.assertTrue(check(self.repo).ok)
        self.assertEqual(gc(self.repo).removed_objects, 1)

    def test_check_finds_missing_and_corrupt_chunks(self):
        self.write("a.bin", os.urandom(2 * SMALL))
        snap = backup(self.repo, [str(self.src)]).snapshot
        first, second = [e for e in snap.entries if e["type"] == "file"][0]["chunks"]
        self.assertTrue(check(self.repo, read_data=True).ok)

        obj = self.repo._object_path(first)
        payload = bytearray(obj.read_bytes())
        payload[-1] ^= 0xFF
        obj.write_bytes(bytes(payload))
        self.repo._object_path(second).unlink()

        quick = check(self.repo)
        self.assertEqual(list(quick.missing), [second])
        self.assertEqual(quick.corrupt, {})  # without --read-data, content isn't hashed
        full = check(self.repo, read_data=True)
        self.assertEqual(list(full.corrupt), [first])
        self.assertEqual(full.damaged_snapshots, [snap.id])
        with self.assertRaises(CorruptObject):
            restore(self.repo, snap, self.tmp / "out")

    def test_restore_refuses_paths_that_escape_the_target(self):
        for bad in ("../escape.txt", "/etc/passwd", "a/../../escape"):
            snap = backup(self.repo, [str(self.write("x", "x").parent)]).snapshot
            snap.entries.append({"path": bad, "type": "file", "mode": 0o644, "mtime_ns": 0, "chunks": []})
            with self.subTest(bad=bad), self.assertRaises(RepoError):
                restore(self.repo, snap, self.tmp / "out")
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_restore_never_writes_through_a_symlink_in_the_target(self):
        self.write("sub/file.txt", "data")
        snap = backup(self.repo, [str(self.src)]).snapshot
        out = self.tmp / "out"
        outside = self.tmp / "outside"
        outside.mkdir()
        # The restore target already contains a symlink where a directory of the snapshot goes.
        first = Path(str(self.src).lstrip("/")).parts[0]
        out.mkdir()
        os.symlink(outside, out / first)
        with self.assertRaises(RepoError):
            restore(self.repo, snap, out)
        self.assertEqual(list(outside.iterdir()), [])

    def test_lock_prevents_concurrent_commands(self):
        with self.repo.lock():
            with self.assertRaises(RepoError):
                with Repository.open(self.repo.path).lock():
                    pass


class HardenedService(Base):
    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("setpriv"), "needs root and setpriv")
    def test_backup_works_with_only_the_capability_the_systemd_unit_grants(self):
        # Regression: O_NOATIME needs CAP_FOWNER, so other users' files were skipped under the unit.
        f = self.write("home/user/private.txt", "x", 0o600)
        for p in (f, f.parent, f.parent.parent):
            os.chown(p, 1000, 1000)
        os.chmod(f.parent, 0o700)
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            ["setpriv", "--inh-caps=-all", "--bounding-set=-all,+dac_read_search", "--",
             sys.executable, "-m", "khs", "backup", "--repo", str(self.repo.path), "--source", str(self.src)],
            cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("skipped", r.stderr)


class RepositoryBasics(unittest.TestCase):
    def test_init_open_and_snapshot_lookup(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "full").mkdir()
            (t / "full" / "x").write_text("x")
            with self.assertRaises(RepoError):
                Repository.init(t / "full")
            with self.assertRaises(RepoError):
                Repository.open(t / "full")
            with self.assertRaises(RepoError):
                Repository.init(t / "r", chunk_size=10)
            repo = Repository.init(t / "r", chunk_size=SMALL)
            self.assertEqual(Repository.open(t / "r").chunk_size, SMALL)
            self.assertEqual(stat.S_IMODE(os.stat(t / "r").st_mode), 0o700)
            with self.assertRaises(RepoError):
                repo.find_snapshot("latest")
            src = t / "s"
            src.mkdir()
            a = backup(repo, [str(src)], now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)).snapshot
            b = backup(repo, [str(src)], now=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)).snapshot
            self.assertEqual(repo.find_snapshot("latest").id, b.id)
            self.assertEqual(repo.find_snapshot(a.id[:20]).id, a.id)
            with self.assertRaises(RepoError):
                repo.find_snapshot("2026")  # ambiguous
            manifest = json.loads((t / "r" / "snapshots" / f"{a.id}.json").read_text())
            self.assertEqual(manifest["format"], 1)


if __name__ == "__main__":
    unittest.main()
