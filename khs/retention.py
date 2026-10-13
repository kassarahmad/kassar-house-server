"""Which snapshots to keep: last N, plus one per day, ISO week, month and year (newest first)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from .repo import RepoError, Repository, Snapshot


@dataclass(frozen=True)
class Policy:
    keep_last: int = 0
    keep_daily: int = 0
    keep_weekly: int = 0
    keep_monthly: int = 0
    keep_yearly: int = 0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or value < 0:
                raise RepoError(f"{name} must be a whole number of 0 or more")

    @property
    def empty(self) -> bool:
        return not any(self.__dict__.values())


def _buckets(tz: dt.tzinfo | None) -> list[tuple[str, Callable[[dt.datetime], object]]]:
    local = lambda t: t.astimezone(tz)  # noqa: E731
    return [
        ("daily", lambda t: local(t).date()),
        ("weekly", lambda t: local(t).isocalendar()[:2]),
        ("monthly", lambda t: (local(t).year, local(t).month)),
        ("yearly", lambda t: local(t).year),
    ]


def select(snapshots: list[Snapshot], policy: Policy, tz: dt.tzinfo | None = None) -> tuple[list[Snapshot], list[Snapshot], dict[str, list[str]]]:
    """Return (keep, remove, reasons). Each rule walks from newest to oldest and keeps the newest
    snapshot of every new day/week/month/year until it has kept as many as requested.
    The newest snapshot is always kept, so a policy can never empty the repository."""
    if policy.empty:
        raise RepoError("refusing to apply an empty retention policy: it would delete every snapshot")
    ordered = sorted(snapshots, key=lambda s: (s.time, s.id), reverse=True)
    reasons: dict[str, list[str]] = {}

    for s in ordered[: policy.keep_last]:
        reasons.setdefault(s.id, []).append("last")
    for name, key in _buckets(tz):
        want = getattr(policy, f"keep_{name}")
        seen: set[object] = set()
        for s in ordered:
            if len(seen) >= want:
                break
            k = key(s.time)
            if k not in seen:
                seen.add(k)
                reasons.setdefault(s.id, []).append(name)
    if ordered and ordered[0].id not in reasons:
        reasons[ordered[0].id] = ["newest"]

    keep = [s for s in ordered if s.id in reasons]
    remove = [s for s in ordered if s.id not in reasons]
    return keep, remove, reasons


@dataclass
class PruneResult:
    removed_snapshots: list[str]
    removed_objects: int
    freed_bytes: int


def forget(
    repo: Repository, policy: Policy, prune: bool = True, dry_run: bool = False, tz: dt.tzinfo | None = None
) -> tuple[list[Snapshot], list[Snapshot], dict[str, list[str]], PruneResult | None]:
    """Apply the policy; with ``prune`` also delete chunks no remaining snapshot uses.
    The caller must hold the repository lock."""
    keep, remove, reasons = select(repo.snapshots(), policy, tz)
    if dry_run:
        return keep, remove, reasons, None
    for s in remove:
        repo.delete_snapshot(s.id)
    result = gc(repo) if prune else None
    if result:
        result.removed_snapshots = [s.id for s in remove]
    return keep, remove, reasons, result


def gc(repo: Repository) -> PruneResult:
    """Delete objects that no snapshot references. Snapshots are read after deletion of the
    forgotten ones, so a chunk shared with a kept snapshot is never removed."""
    used = {d for s in repo.snapshots() for d in s.chunks()}
    removed = freed = 0
    for digest in list(repo.object_digests()):
        if digest not in used:
            freed += repo.delete_object(digest)
            removed += 1
    repo.remove_temp_files()
    return PruneResult(removed_snapshots=[], removed_objects=removed, freed_bytes=freed)
