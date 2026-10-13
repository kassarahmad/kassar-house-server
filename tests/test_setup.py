"""provision/setup.sh against a scratch root, with apt-get, ufw, systemctl and sshd replaced by recorders."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "provision" / "setup.sh"


class Setup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        self.root = t / "root"
        self.root.mkdir()
        self.log = t / "calls.log"
        bin_ = t / "bin"
        bin_.mkdir()
        for cmd in ("apt-get", "ufw", "systemctl", "sshd"):
            p = bin_ / cmd
            p.write_text(f'#!/bin/sh\necho "{cmd} $*" >> "{self.log}"\n' + ('[ "${SSHD_OK:-1}" = 1 ]\n' if cmd == "sshd" else ""))
            p.chmod(0o755)
        self.env = {**os.environ, "KHS_ROOT": str(self.root), "PATH": f"{bin_}:{os.environ['PATH']}", "SUDO_USER": "root"}

    def tearDown(self):
        self._tmp.cleanup()

    def run_setup(self, *args, **env):
        return subprocess.run(["bash", str(SCRIPT), *args], env={**self.env, **env}, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_installs_khs_config_and_timers(self):
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / "opt/khs/khs/cli.py").exists())
        wrapper = self.root / "usr/local/bin/khs"
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        self.assertIn('sys.path.insert(0, "/opt/khs")', wrapper.read_text())
        cfg = self.root / "etc/khs/khs.toml"
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "var/lib/khs").stat().st_mode), 0o700)
        self.assertTrue((self.root / "etc/systemd/system/khs-backup.timer").exists())
        self.assertIn("systemctl daemon-reload", self.calls())
        self.assertIn("systemctl enable --now khs-backup.timer khs-health.timer", self.calls())
        # The installed copy works on its own.
        code = f'import sys; sys.path.insert(0, "{self.root}/opt/khs"); from khs.cli import main; sys.exit(main())'
        v = subprocess.run([sys.executable, "-B", "-c", code, "--version"], capture_output=True, text=True, cwd="/")
        self.assertEqual(v.stdout.strip().split()[0], "khs")

    def test_second_run_changes_nothing_and_keeps_edited_config(self):
        self.run_setup()
        cfg = self.root / "etc/khs/khs.toml"
        cfg.write_text("# my edits\n")
        self.log.unlink()
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(cfg.read_text(), "# my edits\n")
        self.assertNotIn("installed", r.stdout)
        self.assertIn("kept       /etc/khs/khs.toml", r.stdout)
        self.assertNotIn("systemctl daemon-reload", self.calls())

    def test_dry_run_changes_nothing(self):
        r = self.run_setup("--dry-run", "--harden", "192.168.1.0/24")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([p for p in self.root.rglob("*")], [])
        self.assertEqual(self.calls(), [])
        self.assertIn("would run: ufw --force enable", r.stdout)

    def test_harden_opens_only_lan_ports_and_enables_updates(self):
        r = self.run_setup("--harden", "192.168.1.0/24")
        self.assertEqual(r.returncode, 0, r.stderr)
        ufw = [c for c in self.calls() if c.startswith("ufw allow")]
        self.assertEqual(len(ufw), 7)
        self.assertTrue(all("from 192.168.1.0/24" in c for c in ufw))
        self.assertIn("ufw default deny incoming", self.calls())
        self.assertIn('Unattended-Upgrade "1"', (self.root / "etc/apt/apt.conf.d/20auto-upgrades").read_text())

    def test_ssh_is_not_locked_down_without_an_authorized_key(self):
        r = self.run_setup("--harden", "10.0.0.0/8")
        self.assertIn("skipped    key-only SSH", r.stdout)
        self.assertFalse((self.root / "etc/ssh/sshd_config.d/50-house.conf").exists())

    def test_ssh_lockdown_with_a_key_and_rollback_if_sshd_rejects_it(self):
        keys = self.root / "root/.ssh/authorized_keys"
        keys.parent.mkdir(parents=True)
        keys.write_text("ssh-ed25519 AAAA... admin@laptop\n")
        r = self.run_setup("--harden", "10.0.0.0/8")
        self.assertEqual(r.returncode, 0, r.stderr)
        dropin = self.root / "etc/ssh/sshd_config.d/50-house.conf"
        self.assertIn("PasswordAuthentication no", dropin.read_text())
        self.assertIn("systemctl reload ssh", self.calls())

        dropin.unlink()
        r = self.run_setup("--harden", "10.0.0.0/8", SSHD_OK="0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SSH is unchanged", r.stderr)
        self.assertFalse(dropin.exists())

    def test_bad_arguments(self):
        self.assertNotEqual(self.run_setup("--harden", "not-a-cidr").returncode, 0)
        self.assertNotEqual(self.run_setup("--harden", "192.168.1.0/33").returncode, 0)
        self.assertEqual(self.run_setup("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
