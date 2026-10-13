"""Health checks and change notifications.

A notification is sent only when a check changes state (ok → failing, failing → recovered),
so a disk that stays full doesn't page you every five minutes.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import sysinfo
from .config import Config, NotifyConfig
from .repo import RepoError, Repository

OK, FAIL, UNKNOWN = "ok", "fail", "unknown"


@dataclass
class Result:
    name: str
    type: str
    status: str
    detail: str


def _http(p: dict) -> tuple[str, str]:
    ctx = None
    if p["url"].startswith("https://") and p.get("insecure"):
        # For LAN services behind Caddy's internal CA when the CA isn't installed on this host.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(p["url"], headers={"User-Agent": "khs-health"})
    try:
        with urllib.request.urlopen(req, timeout=p["timeout"], context=ctx) as r:
            code, body = r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        code, body = e.code, b""
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return FAIL, f"unreachable: {reason}"
    if code != p["expect"]:
        return FAIL, f"HTTP {code}, expected {p['expect']}"
    if p.get("contains") and p["contains"].encode() not in body:
        return FAIL, f"HTTP {code} but the page doesn't contain {p['contains']!r}"
    return OK, f"HTTP {code}"


def _tcp(p: dict) -> tuple[str, str]:
    try:
        with socket.create_connection((p["host"], p["port"]), timeout=p["timeout"]):
            return OK, f"{p['host']}:{p['port']} accepts connections"
    except OSError as e:
        return FAIL, f"{p['host']}:{p['port']}: {e.strerror or e}"


def _disk(p: dict) -> tuple[str, str]:
    try:
        d = sysinfo.disk_usage(p["path"])
    except OSError as e:
        return FAIL, f"{p['path']}: {e.strerror or e}"
    detail = f"{d.used_pct}% used, {sysinfo.human_bytes(d.free_bytes)} free"
    return (OK if d.used_pct <= p["max_used_pct"] else FAIL), detail


def _systemd(p: dict, run: Callable = subprocess.run) -> tuple[str, str]:
    try:
        r = run(["systemctl", "is-active", "--", p["unit"]], capture_output=True, text=True, timeout=p["timeout"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return UNKNOWN, f"cannot ask systemd: {e}"
    state = (r.stdout or "").strip() or "unknown"
    return (OK if state == "active" else FAIL), state


def _backup_age(p: dict, cfg: Config, now: dt.datetime) -> tuple[str, str]:
    repo_path = p.get("repository") or (cfg.backup.repository if cfg.backup else None)
    if not repo_path:
        return UNKNOWN, "no repository configured"
    try:
        snaps = Repository.open(repo_path).snapshots()
    except RepoError as e:
        return FAIL, str(e)
    if not snaps:
        return FAIL, "no snapshots yet"
    last = snaps[-1]
    hours = (now - last.time).total_seconds() / 3600
    detail = f"last snapshot {hours:.1f} h ago ({last.id})"
    if last.stats.get("skipped"):
        detail += f", {last.stats['skipped']} item(s) skipped"
    return (OK if hours <= p["max_hours"] else FAIL), detail


def _temperature(p: dict, sys_root: Path) -> tuple[str, str]:
    temps = sysinfo.temperatures(sys_root)
    if p.get("zone"):
        temps = {k: v for k, v in temps.items() if k == p["zone"]}
    if not temps:
        return UNKNOWN, "no thermal sensor exposed by the kernel"
    name, hottest = max(temps.items(), key=lambda kv: kv[1])
    return (OK if hottest <= p["max_c"] else FAIL), f"{hottest} °C ({name})"


def _memory(p: dict, proc: Path) -> tuple[str, str]:
    m = sysinfo.meminfo(proc)
    total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    pct = round(100 * (total - avail) / total, 1) if total else 0.0
    return (OK if pct <= p["max_used_pct"] else FAIL), f"{pct}% used"


def run_checks(
    cfg: Config,
    now: dt.datetime | None = None,
    proc: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
    systemd_run: Callable = subprocess.run,
) -> list[Result]:
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for c in cfg.checks:
        try:
            if c.type == "http":
                status, detail = _http(c.params)
            elif c.type == "tcp":
                status, detail = _tcp(c.params)
            elif c.type == "disk":
                status, detail = _disk(c.params)
            elif c.type == "systemd":
                status, detail = _systemd(c.params, systemd_run)
            elif c.type == "backup_age":
                status, detail = _backup_age(c.params, cfg, now)
            elif c.type == "temperature":
                status, detail = _temperature(c.params, sys_root)
            else:
                status, detail = _memory(c.params, proc)
        except Exception as e:  # a broken check must not hide the others
            status, detail = UNKNOWN, f"check error: {e}"
        out.append(Result(c.name, c.type, status, detail))
    return out


# ------------------------------------------------------------ state + notify


def changes(previous: dict[str, str], results: list[Result]) -> tuple[list[Result], list[Result]]:
    """(newly failing, recovered). A check seen for the first time counts as newly failing if it fails."""
    failing = [r for r in results if r.status == FAIL and previous.get(r.name) != FAIL]
    recovered = [r for r in results if r.status == OK and previous.get(r.name) == FAIL]
    return failing, recovered


def load_state(state_dir: str) -> dict[str, str]:
    try:
        return json.loads((Path(state_dir) / "health-state.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state_dir: str, results: list[Result]) -> None:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".health-state.json.tmp"
    tmp.write_text(json.dumps({r.name: r.status for r in results}, indent=2))
    os.replace(tmp, d / "health-state.json")


def build_message(hostname: str, failing: list[Result], recovered: list[Result]) -> tuple[str, str, bool]:
    """(title, body, urgent)."""
    lines = [f"✗ {r.name}: {r.detail}" for r in failing] + [f"✓ {r.name} recovered: {r.detail}" for r in recovered]
    if failing:
        title = f"{hostname}: {len(failing)} check(s) failing"
    else:
        title = f"{hostname}: {len(recovered)} check(s) recovered"
    return title, "\n".join(lines), bool(failing)


def notify(n: NotifyConfig, title: str, body: str, urgent: bool, opener: Callable = urllib.request.urlopen) -> None:
    headers = {"User-Agent": "khs-health"}
    if n.token_file:
        headers["Authorization"] = "Bearer " + Path(n.token_file).read_text().strip()
    if n.format == "json":
        data = json.dumps({"title": title, "message": body, "urgent": urgent}).encode()
        headers["Content-Type"] = "application/json"
    else:  # ntfy: the body is the message, metadata goes in headers (ASCII only)
        data = body.encode()
        headers.update({"Title": title.encode("ascii", "replace").decode(), "Priority": "high" if urgent else "default",
                        "Tags": "warning" if urgent else "white_check_mark"})
    req = urllib.request.Request(n.url, data=data, headers=headers, method="POST")
    with opener(req, timeout=10) as r:
        if r.status >= 300:
            raise OSError(f"notification endpoint answered HTTP {r.status}")


def as_json(results: list[Result]) -> str:
    return json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False)
