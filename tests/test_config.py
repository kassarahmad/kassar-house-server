import unittest
from pathlib import Path

from khs.config import ConfigError, load, parse

EXAMPLE = Path(__file__).resolve().parent.parent / "etc" / "khs.example.toml"


class Config(unittest.TestCase):
    def test_example_config_is_valid(self):
        cfg = load(EXAMPLE)
        self.assertEqual(cfg.backup.sources, ["/srv", "/etc", "/home"])
        self.assertEqual(cfg.backup.retention.keep_daily, 7)
        self.assertEqual(cfg.notify.format, "ntfy")
        self.assertEqual({c.type for c in cfg.checks}, {"http", "tcp", "disk", "systemd", "backup_age", "temperature", "memory"})
        http = next(c for c in cfg.checks if c.type == "http")
        self.assertEqual(http.params["timeout"], 5)

    def test_defaults(self):
        cfg = parse({"backup": {"repository": "/r", "sources": ["/srv/"]}})
        self.assertEqual(cfg.backup.sources, ["/srv"])
        self.assertEqual(cfg.backup.retention.keep_daily, 7)
        self.assertIsNone(cfg.notify)
        self.assertEqual(cfg.checks, [])

    def test_errors_name_the_problem(self):
        cases = {
            "missing `repository`": {"backup": {"sources": ["/srv"]}},
            "absolute path": {"backup": {"repository": "/r", "sources": ["srv"]}},
            "is inside": {"backup": {"repository": "/r", "sources": ["/srv", "/srv/photos"]}},
            "list of strings": {"backup": {"repository": "/r", "sources": "/srv"}},
            "keep_daily": {"backup": {"repository": "/r", "sources": ["/srv"], "retention": {"keep_daily": -1}}},
            "unexpected keyword": {"backup": {"repository": "/r", "sources": ["/srv"], "retention": {"keep_hourly": 1}}},
            "unknown type": {"check": [{"name": "x", "type": "ping"}]},
            "http(s) URL": {"check": [{"name": "x", "type": "http", "url": "ftp://x"}]},
            "`port`": {"check": [{"name": "x", "type": "tcp", "host": "h", "port": 70000}]},
            "duplicate check names": {"check": [{"name": "x", "type": "memory"}, {"name": "x", "type": "memory"}]},
            "`format`": {"notify": {"url": "https://x", "format": "xml"}},
            "[[check]]": {"check": {"name": "x"}},
        }
        for expected, data in cases.items():
            with self.subTest(expected):
                with self.assertRaises(ConfigError) as cm:
                    parse(data)
                self.assertIn(expected, str(cm.exception))

    def test_missing_and_malformed_files(self):
        with self.assertRaises(ConfigError):
            load("/nonexistent/khs.toml")
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write("[backup\n")
            f.flush()
            with self.assertRaises(ConfigError):
                load(f.name)


if __name__ == "__main__":
    unittest.main()
