"""Configuration file (TOML). Every value is validated here so the commands can trust it."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .repo import RepoError
from .retention import Policy

DEFAULT_PATH = Path(os.environ.get("KHS_CONFIG", "/etc/khs/khs.toml"))
CHECK_TYPES = {"http", "tcp", "disk", "systemd", "backup_age", "temperature", "memory"}


class ConfigError(Exception):
    pass


@dataclass
class BackupConfig:
    repository: str
    sources: list[str]
    exclude: list[str] = field(default_factory=list)
    retention: Policy = field(default_factory=lambda: Policy(keep_last=3, keep_daily=7, keep_weekly=4, keep_monthly=6))


@dataclass
class NotifyConfig:
    url: str
    format: str = "ntfy"  # "ntfy" (plain text + headers) or "json"
    token_file: str | None = None


@dataclass
class Check:
    name: str
    type: str
    params: dict[str, Any]


@dataclass
class Config:
    backup: BackupConfig | None
    notify: NotifyConfig | None
    checks: list[Check]
    state_dir: str = "/var/lib/khs"
    path: str = ""


def _req(d: dict, key: str, typ: type, where: str):
    if key not in d:
        raise ConfigError(f"{where}: missing `{key}`")
    v = d[key]
    if not isinstance(v, typ) or isinstance(v, bool) and typ is not bool:
        raise ConfigError(f"{where}: `{key}` must be {typ.__name__}")
    return v


def _str_list(d: dict, key: str, where: str, required: bool = False) -> list[str]:
    v = d.get(key, None if required else [])
    if v is None:
        raise ConfigError(f"{where}: missing `{key}`")
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"{where}: `{key}` must be a list of strings")
    return v


def _number(d: dict, key: str, where: str, default: float | None = None, lo: float = 0, hi: float | None = None) -> float:
    if key not in d:
        if default is None:
            raise ConfigError(f"{where}: missing `{key}`")
        return default
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo or (hi is not None and v > hi):
        raise ConfigError(f"{where}: `{key}` must be a number between {lo} and {hi if hi is not None else 'any'}")
    return v


def _validate_check(i: int, c: dict) -> Check:
    where = f"check #{i + 1}"
    name = _req(c, "name", str, where)
    where = f"check {name!r}"
    typ = _req(c, "type", str, where)
    if typ not in CHECK_TYPES:
        raise ConfigError(f"{where}: unknown type {typ!r} (use one of {', '.join(sorted(CHECK_TYPES))})")
    p: dict[str, Any] = {"timeout": _number(c, "timeout", where, 5, 0.1, 120)}
    if typ == "http":
        url = _req(c, "url", str, where)
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ConfigError(f"{where}: `url` must be an http(s) URL")
        p.update(url=url, expect=int(_number(c, "expect", where, 200, 100, 599)), contains=c.get("contains"),
                 insecure=bool(c.get("insecure", False)))
    elif typ == "tcp":
        p.update(host=_req(c, "host", str, where), port=int(_number(c, "port", where, None, 1, 65535)))
    elif typ == "disk":
        p.update(path=_req(c, "path", str, where), max_used_pct=_number(c, "max_used_pct", where, 90, 1, 100))
    elif typ == "systemd":
        p.update(unit=_req(c, "unit", str, where))
    elif typ == "backup_age":
        p.update(repository=c.get("repository"), max_hours=_number(c, "max_hours", where, 26, 1))
    elif typ == "temperature":
        p.update(max_c=_number(c, "max_c", where, 75, 20, 120), zone=c.get("zone"))
    elif typ == "memory":
        p.update(max_used_pct=_number(c, "max_used_pct", where, 90, 1, 100))
    return Check(name, typ, p)


def parse(data: dict[str, Any], path: str = "") -> Config:
    backup = None
    if "backup" in data:
        b = data["backup"]
        repo = _req(b, "repository", str, "[backup]")
        sources = _str_list(b, "sources", "[backup]", required=True)
        if not sources:
            raise ConfigError("[backup]: `sources` is empty")
        for s in sources:
            if not s.startswith("/"):
                raise ConfigError(f"[backup]: source {s!r} must be an absolute path")
        norm = [s.rstrip("/") or "/" for s in sources]
        for a in norm:
            for b2 in norm:
                if a != b2 and b2.startswith(a.rstrip("/") + "/"):
                    raise ConfigError(f"[backup]: {b2} is inside {a}; list it once")
        r = b.get("retention", {})
        if not isinstance(r, dict):
            raise ConfigError("[backup.retention] must be a table")
        try:
            policy = Policy(**r)
        except (TypeError, RepoError) as e:
            raise ConfigError(f"[backup.retention]: {e}") from None
        backup = BackupConfig(repo, norm, _str_list(b, "exclude", "[backup]"))
        if r:
            backup.retention = policy
    notify = None
    if "notify" in data:
        n = data["notify"]
        url = _req(n, "url", str, "[notify]")
        if urlparse(url).scheme not in ("http", "https"):
            raise ConfigError("[notify]: `url` must be an http(s) URL")
        fmt = n.get("format", "ntfy")
        if fmt not in ("ntfy", "json"):
            raise ConfigError("[notify]: `format` must be \"ntfy\" or \"json\"")
        notify = NotifyConfig(url, fmt, n.get("token_file"))
    raw_checks = data.get("check", [])
    if not isinstance(raw_checks, list):
        raise ConfigError("use [[check]] tables for checks")
    checks = [_validate_check(i, c) for i, c in enumerate(raw_checks)]
    names = [c.name for c in checks]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate check names: {', '.join(sorted(dupes))}")
    return Config(backup, notify, checks, str(data.get("state_dir", "/var/lib/khs")), path)


def load(path: str | os.PathLike[str] | None = None) -> Config:
    p = Path(path) if path else DEFAULT_PATH
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from None
    return parse(data, str(p))
