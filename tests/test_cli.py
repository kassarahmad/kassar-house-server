import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khs.cli import main


def run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self._tmp.name)
        self.src = t / "srv"
        (self.src / "photos").mkdir(parents=True)
        (self.src / "photos" / "a.jpg").write_bytes(os.urandom(5000))
        (self.src / "notes.md").write_text("# notes")
        self.repo = t / "repo"
        self.cfg = t / "khs.toml"
        self.cfg.write_text(
            f'state_dir = "{t / "state"}"\n'
            f'[backup]\nrepository = "{self.repo}"\nsources = ["{self.src}"]\nexclude = ["*.tmp"]\n'
            '[backup.retention]\nkeep_last = 1\n'
            '[notify]\nurl = "https://ntfy.example/topic"\n'
            f'[[check]]\nname = "Data disk"\ntype = "disk"\npath = "{t}"\nmax_used_pct = 100\n'
            f'[[check]]\nname = "Backup"\ntype = "backup_age"\nmax_hours = 26\n'
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_backup_restore_check_cycle_from_config(self):
        self.assertEqual(run("init", str(self.repo))[0], 0)
        code, out, _ = run("backup", "--config", str(self.cfg))
        self.assertEqual(code, 0, out)
        self.assertIn("2 files", out)

        code, out, _ = run("snapshots", "--config", str(self.cfg), "--json")
        snaps = json.loads(out)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["sources"], [str(self.src)])

        target = self.t / "restore"
        code, out, _ = run("restore", "latest", "--config", str(self.cfg), "--target", str(target))
        self.assertEqual(code, 0)
        restored = target / str(self.src).lstrip("/")
        self.assertEqual((restored / "notes.md").read_text(), "# notes")
        self.assertEqual((restored / "photos" / "a.jpg").read_bytes(), (self.src / "photos" / "a.jpg").read_bytes())

        self.assertEqual(run("check", "--config", str(self.cfg), "--read-data")[0], 0)
        obj = next(p for p in (self.repo / "objects").rglob("*") if p.is_file())
        obj.write_bytes(b"R" + b"tampered")
        code, _, err = run("check", "--config", str(self.cfg), "--read-data")
        self.assertEqual(code, 1)
        self.assertIn("CORRUPT", err)

    def test_partial_backup_exit_code_and_forget(self):
        run("init", str(self.repo))
        os.mkfifo(self.src / "pipe")
        code, _, err = run("backup", "--repo", str(self.repo), "--source", str(self.src))
        self.assertEqual(code, 3)
        self.assertIn("special file", err)
        run("backup", "--repo", str(self.repo), "--source", str(self.src))
        code, out, _ = run("forget", "--config", str(self.cfg), "--prune")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("keep "), 1)
        self.assertEqual(out.count("removed "), 1)

    def test_errors_are_short_messages_not_tracebacks(self):
        code, _, err = run("snapshots", "--repo", str(self.t / "nothing"))
        self.assertEqual((code, err.strip()), (1, f"khs: {self.t / 'nothing'} is not a khs repository (run `khs init {self.t / 'nothing'}` first)"))
        bad = self.t / "bad.toml"
        bad.write_text('[[check]]\nname = "x"\ntype = "ping"\n')
        code, _, err = run("config-check", "--config", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("unknown type 'ping'", err)
        self.assertEqual(run("config-check", "--config", str(self.cfg))[0], 0)

    def test_health_exit_code_and_notifications_only_on_change(self):
        with mock.patch("khs.health.notify") as notify:
            code, out, _ = run("health", "--config", str(self.cfg), "--notify")
            self.assertEqual(code, 1)  # no repository yet, so the backup check fails
            self.assertIn("FAIL  Backup", out)
            self.assertEqual(notify.call_count, 1)
            self.assertIn("1 check(s) failing", notify.call_args.args[1])

            run("health", "--config", str(self.cfg), "--notify")
            self.assertEqual(notify.call_count, 1)  # still failing: no new message

            run("init", str(self.repo))
            run("backup", "--config", str(self.cfg))
            code, _, _ = run("health", "--config", str(self.cfg), "--notify")
            self.assertEqual(code, 0)
            self.assertEqual(notify.call_count, 2)
            self.assertIn("recovered", notify.call_args.args[1])

    def test_failed_notification_is_retried_next_time(self):
        with mock.patch("khs.health.notify", side_effect=OSError("ntfy down")) as notify:
            code, _, err = run("health", "--config", str(self.cfg), "--notify")
            self.assertEqual(code, 1)
            self.assertIn("notification failed", err)
        self.assertFalse((self.t / "state" / "health-state.json").exists())
        with mock.patch("khs.health.notify") as notify:
            run("health", "--config", str(self.cfg), "--notify")
            self.assertEqual(notify.call_count, 1)

    def test_health_json_and_report(self):
        code, out, _ = run("health", "--config", str(self.cfg), "--json")
        self.assertEqual({r["name"] for r in json.loads(out)}, {"Data disk", "Backup"})
        code, out, _ = run("report", "--json")
        self.assertEqual(code, 0)
        self.assertIn("mem_used_pct", json.loads(out))


if __name__ == "__main__":
    unittest.main()
