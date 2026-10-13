"""Read the machine's own numbers from /proc and /sys (Linux). Paths are parameters so tests can use fixtures."""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class DiskUsage:
    mount: str
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def used_pct(self) -> float:
        """Same convention as `df`: space reserved for root counts as unavailable."""
        usable = self.used_bytes + self.free_bytes
        return round(100 * self.used_bytes / usable, 1) if usable else 0.0


@dataclass
class Report:
    hostname: str
    uptime_sec: float
    load: tuple[float, float, float]
    cpu_cores: int
    cpu_used_pct: float
    mem_total_bytes: int
    mem_available_bytes: int
    swap_used_pct: float
    disks: list[DiskUsage]
    temperatures_c: dict[str, float]

    @property
    def mem_used_pct(self) -> float:
        t = self.mem_total_bytes
        return round(100 * (t - self.mem_available_bytes) / t, 1) if t else 0.0

    def to_json(self) -> dict:
        d = asdict(self)
        d["mem_used_pct"] = self.mem_used_pct
        d["disks"] = [{**asdict(x), "used_pct": x.used_pct} for x in self.disks]
        return d


def meminfo(proc: Path = Path("/proc")) -> dict[str, int]:
    out = {}
    for line in (proc / "meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def cpu_times(proc: Path = Path("/proc")) -> tuple[int, int]:
    """(idle, total) jiffies from the aggregate cpu line."""
    first = (proc / "stat").read_text().splitlines()[0].split()
    vals = [int(v) for v in first[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return idle, sum(vals[:8])


def cpu_used_pct(proc: Path = Path("/proc"), interval: float = 0.5) -> float:
    i1, t1 = cpu_times(proc)
    time.sleep(interval)
    i2, t2 = cpu_times(proc)
    dt = t2 - t1
    return round(100 * (1 - (i2 - i1) / dt), 1) if dt > 0 else 0.0


def temperatures(sys_root: Path = Path("/sys")) -> dict[str, float]:
    """Thermal zones in °C (Raspberry Pi, most x86 boards). Empty when the kernel exposes none."""
    out: dict[str, float] = {}
    for zone in sorted((sys_root / "class" / "thermal").glob("thermal_zone*")):
        try:
            name = (zone / "type").read_text().strip() or zone.name
            out[name] = round(int((zone / "temp").read_text().strip()) / 1000, 1)
        except (OSError, ValueError):
            continue
    return out


def disk_usage(mount: str) -> DiskUsage:
    st = os.statvfs(mount)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return DiskUsage(mount, total, used, free)


def collect(mounts: list[str] | None = None, proc: Path = Path("/proc"), sys_root: Path = Path("/sys"), cpu_interval: float = 0.5) -> Report:
    mem = meminfo(proc)
    swap_total = mem.get("SwapTotal", 0)
    load = tuple(float(x) for x in (proc / "loadavg").read_text().split()[:3])
    return Report(
        hostname=os.uname().nodename,
        uptime_sec=float((proc / "uptime").read_text().split()[0]),
        load=load,  # type: ignore[arg-type]
        cpu_cores=os.cpu_count() or 1,
        cpu_used_pct=cpu_used_pct(proc, cpu_interval),
        mem_total_bytes=mem.get("MemTotal", 0),
        mem_available_bytes=mem.get("MemAvailable", mem.get("MemFree", 0)),
        swap_used_pct=round(100 * (swap_total - mem.get("SwapFree", 0)) / swap_total, 1) if swap_total else 0.0,
        disks=[disk_usage(m) for m in (mounts or ["/"])],
        temperatures_c=temperatures(sys_root),
    )


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"
