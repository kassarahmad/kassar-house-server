"""`khs` command line. Exit codes: 0 ok, 1 error or failing check, 2 usage error, 3 backup finished with skipped files."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import __version__, health, sysinfo
from .backup import backup, check, restore
from .config import DEFAULT_PATH, Config, ConfigError, load
from .repo import RepoError, Repository
from .retention import Policy, forget, gc

EXIT_OK, EXIT_ERR, EXIT_PARTIAL = 0, 1, 3


def _cfg(args) -> Config | None:
    """The config file named with --config, else the default one if it exists, else None."""
    if getattr(args, "config", None):
        return load(args.config)
    return load() if DEFAULT_PATH.exists() else None


def _repo_path(args, cfg: Config | None) -> str:
    if getattr(args, "repo", None):
        return args.repo
    if cfg and cfg.backup:
        return cfg.backup.repository
    raise ConfigError("no repository: pass --repo or set [backup] repository in the config")


def cmd_init(args) -> int:
    repo = Repository.init(args.path)
    print(f"initialised repository at {repo.path} (chunk size {sysinfo.human_bytes(repo.chunk_size)})")
    return EXIT_OK


def cmd_backup(args) -> int:
    cfg = _cfg(args)
    bcfg = cfg.backup if cfg else None
    if not args.source and not bcfg:
        raise ConfigError("nothing to back up: pass --source or set [backup] sources in the config")
    repo = Repository.open(_repo_path(args, cfg))
    sources = args.source or bcfg.sources
    excludes = (args.exclude or []) + (bcfg.exclude if bcfg and not args.source else [])
    with repo.lock():
        res = backup(repo, sources, excludes, tags=args.tag or [])
    s = res.snapshot
    print(f"snapshot {s.id}: {res.files} files ({res.unchanged} unchanged), {res.dirs} dirs, {res.symlinks} symlinks")
    print(f"read {sysinfo.human_bytes(res.bytes_read)}, stored {sysinfo.human_bytes(res.bytes_stored)} new")
    for msg in res.skipped:
        print(f"skipped: {msg}", file=sys.stderr)
    for p in res.changed_during_backup:
        print(f"changed while being read (saved as read): {p}", file=sys.stderr)
    return EXIT_PARTIAL if res.partial else EXIT_OK


def cmd_snapshots(args) -> int:
    repo = Repository.open(_repo_path(args, _cfg(args)))
    snaps = repo.snapshots()
    if args.json:
        print(json.dumps([{"id": s.id, "time": s.time.isoformat(), "hostname": s.hostname, "sources": s.sources,
                           "tags": s.tags, "stats": s.stats} for s in snaps], indent=2))
        return EXIT_OK
    if not snaps:
        print("no snapshots")
    for s in snaps:
        st = s.stats
        print(f"{s.id}  {s.time.astimezone():%Y-%m-%d %H:%M}  {st.get('files', 0):>7} files  "
              f"{sysinfo.human_bytes(st.get('total_size', 0)):>10}  {', '.join(s.sources)}{'  [' + ','.join(s.tags) + ']' if s.tags else ''}")
    return EXIT_OK


def cmd_restore(args) -> int:
    repo = Repository.open(_repo_path(args, _cfg(args)))
    snap = repo.find_snapshot(args.snapshot)
    res = restore(repo, snap, args.target, include=args.include, overwrite=args.overwrite,
                  owners=False if args.no_owners else None)
    print(f"restored {res.files} files, {res.dirs} dirs, {res.symlinks} symlinks ({sysinfo.human_bytes(res.bytes_written)}) "
          f"from {snap.id} into {Path(args.target).resolve()}")
    if res.skipped_existing:
        print(f"{len(res.skipped_existing)} existing path(s) left untouched (use --overwrite to replace them)", file=sys.stderr)
    return EXIT_OK


def cmd_check(args) -> int:
    repo = Repository.open(_repo_path(args, _cfg(args)))
    with repo.lock():
        res = check(repo, read_data=args.read_data)
    print(f"{res.snapshots} snapshots, {res.objects_checked} chunks {'re-hashed' if args.read_data else 'present'}, "
          f"{res.unreferenced} unreferenced")
    for d, ids in sorted(res.missing.items()):
        print(f"MISSING {d[:16]} used by {', '.join(ids)}", file=sys.stderr)
    for d, ids in sorted(res.corrupt.items()):
        print(f"CORRUPT {d[:16]} used by {', '.join(ids)}", file=sys.stderr)
    if not res.ok:
        print(f"damaged snapshots: {', '.join(res.damaged_snapshots)}", file=sys.stderr)
    return EXIT_OK if res.ok else EXIT_ERR


def cmd_forget(args) -> int:
    cfg = _cfg(args)
    repo = Repository.open(_repo_path(args, cfg))
    given = {k: getattr(args, k) for k in ("keep_last", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly") if getattr(args, k) is not None}
    policy = Policy(**given) if given else (cfg.backup.retention if cfg and cfg.backup else Policy())
    with repo.lock():
        keep, remove, reasons, pruned = forget(repo, policy, prune=args.prune, dry_run=args.dry_run)
    for s in keep:
        print(f"keep    {s.id}  ({', '.join(reasons[s.id])})")
    for s in remove:
        print(f"{'would remove' if args.dry_run else 'removed'} {s.id}")
    if pruned:
        print(f"pruned {pruned.removed_objects} chunks, freed {sysinfo.human_bytes(pruned.freed_bytes)}")
    return EXIT_OK


def cmd_prune(args) -> int:
    repo = Repository.open(_repo_path(args, _cfg(args)))
    with repo.lock():
        res = gc(repo)
    print(f"pruned {res.removed_objects} chunks, freed {sysinfo.human_bytes(res.freed_bytes)}")
    return EXIT_OK


def cmd_health(args) -> int:
    cfg = _cfg(args)
    if cfg is None:
        raise ConfigError(f"config file not found: {DEFAULT_PATH} (pass --config)")
    results = health.run_checks(cfg)
    if args.json:
        print(health.as_json(results))
    else:
        width = max((len(r.name) for r in results), default=4)
        for r in results:
            mark = {"ok": "OK  ", "fail": "FAIL", "unknown": "??  "}[r.status]
            print(f"{mark}  {r.name:<{width}}  {r.detail}")
    if args.notify:
        prev = health.load_state(cfg.state_dir)
        failing, recovered = health.changes(prev, results)
        if (failing or recovered) and cfg.notify:
            title, body, urgent = health.build_message(sysinfo.collect(cpu_interval=0).hostname, failing, recovered)
            try:
                health.notify(cfg.notify, title, body, urgent)
            except OSError as e:
                print(f"notification failed: {e}", file=sys.stderr)
                return EXIT_ERR  # state not saved, so the next run tries again
        health.save_state(cfg.state_dir, results)
    return EXIT_ERR if any(r.status == health.FAIL for r in results) else EXIT_OK


def cmd_report(args) -> int:
    mounts = args.mount or ["/"]
    rep = sysinfo.collect(mounts)
    if args.json:
        print(json.dumps(rep.to_json(), indent=2))
        return EXIT_OK
    hb = sysinfo.human_bytes
    up = dt.timedelta(seconds=int(rep.uptime_sec))
    print(f"host      {rep.hostname}, up {up}")
    print(f"cpu       {rep.cpu_used_pct}% of {rep.cpu_cores} cores, load {' '.join(f'{x:.2f}' for x in rep.load)}")
    print(f"memory    {rep.mem_used_pct}% of {hb(rep.mem_total_bytes)}, swap {rep.swap_used_pct}%")
    for d in rep.disks:
        print(f"disk      {d.mount}: {d.used_pct}% of {hb(d.total_bytes)}, {hb(d.free_bytes)} free")
    for name, t in rep.temperatures_c.items():
        print(f"temp      {name}: {t} °C")
    if not rep.temperatures_c:
        print("temp      no thermal sensor exposed")
    return EXIT_OK


def cmd_config_check(args) -> int:
    cfg = load(args.config)
    print(f"{cfg.path}: OK ({len(cfg.checks)} checks, backup {'configured' if cfg.backup else 'not configured'}, "
          f"notifications {'on' if cfg.notify else 'off'})")
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="khs", description="Backups, health checks and a system report for a home server.")
    p.add_argument("--version", action="version", version=f"khs {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_repo(sp):
        sp.add_argument("--config", help="config file (default /etc/khs/khs.toml or $KHS_CONFIG)")
        sp.add_argument("--repo", help="repository path (overrides the config)")
        return sp

    sp = sub.add_parser("init", help="create an empty repository")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_init)

    sp = with_repo(sub.add_parser("backup", help="take a snapshot"))
    sp.add_argument("--source", action="append", help="directory or file (repeatable; overrides the config)")
    sp.add_argument("--exclude", action="append", help="glob pattern (repeatable)")
    sp.add_argument("--tag", action="append")
    sp.set_defaults(fn=cmd_backup)

    sp = with_repo(sub.add_parser("snapshots", help="list snapshots"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_snapshots)

    sp = with_repo(sub.add_parser("restore", help="restore a snapshot into a directory"))
    sp.add_argument("snapshot", help="snapshot id, unique prefix, or `latest`")
    sp.add_argument("--target", required=True)
    sp.add_argument("--include", help="restore only this path (as stored, e.g. srv/photos)")
    sp.add_argument("--overwrite", action="store_true", help="replace existing files")
    sp.add_argument("--no-owners", action="store_true", help="don't restore file owners (default: restore them when run as root)")
    sp.set_defaults(fn=cmd_restore)

    sp = with_repo(sub.add_parser("check", help="verify that every snapshot's data is present"))
    sp.add_argument("--read-data", action="store_true", help="also re-hash every chunk (slow)")
    sp.set_defaults(fn=cmd_check)

    sp = with_repo(sub.add_parser("forget", help="apply the retention policy"))
    for k in ("last", "daily", "weekly", "monthly", "yearly"):
        sp.add_argument(f"--keep-{k}", type=int, dest=f"keep_{k}")
    sp.add_argument("--prune", action="store_true", help="also delete data no snapshot uses")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_forget)

    sp = with_repo(sub.add_parser("prune", help="delete data no snapshot uses"))
    sp.set_defaults(fn=cmd_prune)

    sp = sub.add_parser("health", help="run the configured checks")
    sp.add_argument("--config")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--notify", action="store_true", help="send a notification when a check changes state")
    sp.set_defaults(fn=cmd_health)

    sp = sub.add_parser("report", help="CPU, memory, disks and temperatures")
    sp.add_argument("--mount", action="append")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("config-check", help="validate the config file")
    sp.add_argument("--config")
    sp.set_defaults(fn=cmd_config_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except (RepoError, ConfigError) as e:
        print(f"khs: {e}", file=sys.stderr)
        return EXIT_ERR
    except KeyboardInterrupt:
        print("khs: interrupted; the repository is unchanged except for unreferenced chunks (`khs prune` removes them)", file=sys.stderr)
        return 130
