import datetime as dt
import http.server
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from khs import health, sysinfo
from khs.backup import backup
from khs.config import NotifyConfig, parse
from khs.repo import Repository

UTC = dt.timezone.utc


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = 500 if self.path == "/broken" else 200
        body = b"<h1>Syncthing</h1>" if code == 200 else b"oops"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def write_fixture_proc(root: Path, mem_total_kb=8_000_000, mem_avail_kb=2_000_000):
    root.mkdir(parents=True, exist_ok=True)
    (root / "meminfo").write_text(
        f"MemTotal:       {mem_total_kb} kB\nMemFree:          100000 kB\nMemAvailable:   {mem_avail_kb} kB\n"
        "SwapTotal:       1000000 kB\nSwapFree:         750000 kB\n"
    )
    (root / "stat").write_text("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\n")
    (root / "loadavg").write_text("0.50 0.40 0.30 1/200 1234\n")
    (root / "uptime").write_text("3600.50 7000.00\n")


def write_fixture_sys(root: Path, temps: dict[str, int]):
    for i, (name, milli) in enumerate(temps.items()):
        z = root / "class" / "thermal" / f"thermal_zone{i}"
        z.mkdir(parents=True)
        (z / "type").write_text(name + "\n")
        (z / "temp").write_text(f"{milli}\n")


class Checks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_one(self, check: dict, **kw):
        cfg = parse({"check": [{"name": "c", **check}], **kw.pop("extra", {})})
        return health.run_checks(cfg, **kw)[0]

    def test_http_status_and_content(self):
        base = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.run_one({"type": "http", "url": base + "/"}).status, "ok")
        r = self.run_one({"type": "http", "url": base + "/broken"})
        self.assertEqual((r.status, r.detail), ("fail", "HTTP 500, expected 200"))
        self.assertEqual(self.run_one({"type": "http", "url": base + "/broken", "expect": 500}).status, "ok")
        self.assertEqual(self.run_one({"type": "http", "url": base + "/", "contains": "Syncthing"}).status, "ok")
        self.assertEqual(self.run_one({"type": "http", "url": base + "/", "contains": "Jellyfin"}).status, "fail")

    def test_tcp_open_and_closed_ports(self):
        self.assertEqual(self.run_one({"type": "tcp", "host": "127.0.0.1", "port": self.port}).status, "ok")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        r = self.run_one({"type": "tcp", "host": "127.0.0.1", "port": closed, "timeout": 1})
        self.assertEqual(r.status, "fail")

    def test_disk_threshold(self):
        used = sysinfo.disk_usage(str(self.tmp)).used_pct
        self.assertEqual(self.run_one({"type": "disk", "path": str(self.tmp), "max_used_pct": 100}).status, "ok")
        if used > 1:
            self.assertEqual(self.run_one({"type": "disk", "path": str(self.tmp), "max_used_pct": 1}).status, "fail")
        self.assertEqual(self.run_one({"type": "disk", "path": "/nonexistent"}).status, "fail")

    def test_systemd_states(self):
        def fake(state):
            return lambda *a, **k: subprocess.CompletedProcess(a, 0 if state == "active" else 3, stdout=state + "\n")

        self.assertEqual(self.run_one({"type": "systemd", "unit": "docker.service"}, systemd_run=fake("active")).status, "ok")
        r = self.run_one({"type": "systemd", "unit": "docker.service"}, systemd_run=fake("failed"))
        self.assertEqual((r.status, r.detail), ("fail", "failed"))

        def missing(*a, **k):
            raise FileNotFoundError("systemctl")

        self.assertEqual(self.run_one({"type": "systemd", "unit": "x"}, systemd_run=missing).status, "unknown")

    def test_backup_age(self):
        repo = Repository.init(self.tmp / "repo")
        extra = {"backup": {"repository": str(repo.path), "sources": [str(self.tmp)]}}
        r = self.run_one({"type": "backup_age"}, extra=extra)
        self.assertEqual((r.status, r.detail), ("fail", "no snapshots yet"))
        src = self.tmp / "src"
        src.mkdir()
        backup(repo, [str(src)], now=dt.datetime(2026, 1, 1, 3, tzinfo=UTC))
        now = dt.datetime(2026, 1, 1, 20, tzinfo=UTC)
        self.assertEqual(self.run_one({"type": "backup_age", "max_hours": 26}, extra=extra, now=now).status, "ok")
        later = dt.datetime(2026, 1, 3, tzinfo=UTC)
        self.assertEqual(self.run_one({"type": "backup_age", "max_hours": 26}, extra=extra, now=later).status, "fail")
        self.assertEqual(self.run_one({"type": "backup_age", "repository": "/nonexistent"}).status, "fail")

    def test_temperature_and_memory_from_fixtures(self):
        sys_root = self.tmp / "sys"
        write_fixture_sys(sys_root, {"x86_pkg_temp": 81000, "acpitz": 45000})
        r = self.run_one({"type": "temperature", "max_c": 75}, sys_root=sys_root)
        self.assertEqual((r.status, r.detail), ("fail", "81.0 °C (x86_pkg_temp)"))
        self.assertEqual(self.run_one({"type": "temperature", "zone": "acpitz", "max_c": 75}, sys_root=sys_root).status, "ok")
        self.assertEqual(self.run_one({"type": "temperature"}, sys_root=self.tmp / "empty").status, "unknown")

        proc = self.tmp / "proc"
        write_fixture_proc(proc)
        r = self.run_one({"type": "memory", "max_used_pct": 70}, proc=proc)
        self.assertEqual((r.status, r.detail), ("fail", "75.0% used"))

    def test_one_broken_check_does_not_hide_the_others(self):
        cfg = parse({"check": [{"name": "a", "type": "memory"}, {"name": "b", "type": "memory"}]})
        res = health.run_checks(cfg, proc=self.tmp / "missing")
        self.assertEqual([r.status for r in res], ["unknown", "unknown"])


class Notifications(unittest.TestCase):
    def r(self, name, status):
        return health.Result(name, "x", status, f"{name} is {status}")

    def test_only_state_changes_are_reported(self):
        prev = {"disk": "ok", "web": "fail", "dns": "fail"}
        results = [self.r("disk", "fail"), self.r("web", "fail"), self.r("dns", "ok"), self.r("new", "fail"), self.r("temp", "unknown")]
        failing, recovered = health.changes(prev, results)
        self.assertEqual([x.name for x in failing], ["disk", "new"])
        self.assertEqual([x.name for x in recovered], ["dns"])
        title, body, urgent = health.build_message("nas", failing, recovered)
        self.assertEqual(title, "nas: 2 check(s) failing")
        self.assertTrue(urgent)
        self.assertIn("✓ dns recovered", body)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(health.load_state(t), {})
            health.save_state(t, [self.r("a", "ok"), self.r("b", "fail")])
            self.assertEqual(health.load_state(t), {"a": "ok", "b": "fail"})

    def capture(self):
        sent = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            sent.append(req)
            return Resp()

        return sent, opener

    def test_ntfy_and_json_formats_and_token(self):
        sent, opener = self.capture()
        health.notify(NotifyConfig("https://ntfy.example/topic"), "nas: 1 check(s) failing", "✗ disk: 95%", True, opener)
        req = sent[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data.decode(), "✗ disk: 95%")
        self.assertEqual(req.get_header("Priority"), "high")
        self.assertEqual(req.get_header("Title"), "nas: 1 check(s) failing")

        with tempfile.NamedTemporaryFile("w") as tok:
            tok.write("secret-token\n")
            tok.flush()
            health.notify(NotifyConfig("https://hook.example", "json", tok.name), "t", "b", False, opener)
        req = sent[1]
        self.assertEqual(json.loads(req.data), {"title": "t", "message": "b", "urgent": False})
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")


class SysInfo(unittest.TestCase):
    def test_report_from_fixtures(self):
        with tempfile.TemporaryDirectory() as t:
            proc, sys_root = Path(t) / "proc", Path(t) / "sys"
            write_fixture_proc(proc)
            write_fixture_sys(sys_root, {"cpu-thermal": 52350})
            rep = sysinfo.collect(["/"], proc=proc, sys_root=sys_root, cpu_interval=0)
            self.assertEqual(rep.load, (0.5, 0.4, 0.3))
            self.assertEqual(rep.uptime_sec, 3600.5)
            self.assertEqual(rep.mem_used_pct, 75.0)
            self.assertEqual(rep.swap_used_pct, 25.0)
            self.assertEqual(rep.temperatures_c, {"cpu-thermal": 52.4})
            self.assertEqual(sysinfo.cpu_times(proc), (800, 1000))
            json.dumps(rep.to_json())

    def test_real_machine(self):
        rep = sysinfo.collect(["/"], cpu_interval=0.1)
        self.assertGreater(rep.mem_total_bytes, 0)
        self.assertTrue(0 <= rep.cpu_used_pct <= 100)
        self.assertTrue(0 <= rep.disks[0].used_pct <= 100)

    def test_human_bytes(self):
        self.assertEqual(sysinfo.human_bytes(512), "512 B")
        self.assertEqual(sysinfo.human_bytes(1536), "1.5 KiB")
        self.assertEqual(sysinfo.human_bytes(5 * 1024**3), "5.0 GiB")


if __name__ == "__main__":
    unittest.main()
