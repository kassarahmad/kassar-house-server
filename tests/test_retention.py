import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from khs.backup import backup, check
from khs.repo import RepoError, Repository, Snapshot
from khs.retention import Policy, forget, select

UTC = dt.timezone.utc


def snaps(times):
    return [Snapshot(id=f"s{i:03d}", time=t, hostname="h", sources=["/x"], entries=[]) for i, t in enumerate(times)]


class Select(unittest.TestCase):
    def test_daily_keeps_newest_snapshot_of_each_day(self):
        base = dt.datetime(2026, 3, 1, tzinfo=UTC)
        times = [base + dt.timedelta(days=d, hours=h) for d in range(10) for h in (2, 14)]
        keep, remove, reasons = select(snaps(times), Policy(keep_daily=7), UTC)
        kept_times = sorted(s.time for s in keep)
        self.assertEqual(len(keep), 7)
        self.assertTrue(all(t.hour == 14 for t in kept_times))  # newest of each day
        self.assertEqual(kept_times[0].date(), dt.date(2026, 3, 4))
        self.assertEqual(len(remove), 20 - 7)

    def test_rules_combine_and_explain_themselves(self):
        start = dt.datetime(2025, 1, 1, 3, tzinfo=UTC)
        times = [start + dt.timedelta(days=d) for d in range(400)]  # one per day for 400 days
        keep, _, reasons = select(snaps(times), Policy(keep_last=2, keep_daily=7, keep_weekly=4, keep_monthly=12, keep_yearly=3), UTC)
        newest = max(times)
        by_id = {s.id: s for s in keep}
        self.assertEqual(sorted(reasons[s.id] for s in keep if s.time == newest)[0], ["last", "daily", "weekly", "monthly", "yearly"])
        monthly = sorted({(by_id[i].time.year, by_id[i].time.month) for i, r in reasons.items() if "monthly" in r})
        self.assertEqual(len(monthly), 12)
        yearly = sorted(by_id[i].time.year for i, r in reasons.items() if "yearly" in r)
        self.assertEqual(yearly, [2025, 2026])  # only two years exist
        self.assertLessEqual(len(keep), 2 + 7 + 4 + 12 + 3)

    def test_weekly_uses_iso_weeks(self):
        # Sunday 2026-01-04 and Monday 2026-01-05 are in different ISO weeks.
        times = [dt.datetime(2026, 1, 4, 12, tzinfo=UTC), dt.datetime(2026, 1, 5, 12, tzinfo=UTC)]
        keep, _, _ = select(snaps(times), Policy(keep_weekly=2), UTC)
        self.assertEqual(len(keep), 2)

    def test_local_time_decides_the_day(self):
        rome = dt.timezone(dt.timedelta(hours=1))
        # 23:30 UTC on the 1st is already the 2nd in Rome.
        times = [dt.datetime(2026, 1, 1, 22, 0, tzinfo=UTC), dt.datetime(2026, 1, 1, 23, 30, tzinfo=UTC)]
        self.assertEqual(len(select(snaps(times), Policy(keep_daily=5), UTC)[0]), 1)
        self.assertEqual(len(select(snaps(times), Policy(keep_daily=5), rome)[0]), 2)

    def test_empty_or_invalid_policy_is_refused_and_newest_always_kept(self):
        with self.assertRaises(RepoError):
            select(snaps([dt.datetime.now(UTC)]), Policy(), UTC)
        with self.assertRaises(RepoError):
            Policy(keep_daily=-1)
        times = [dt.datetime(2020, 1, 1, tzinfo=UTC), dt.datetime(2026, 1, 1, tzinfo=UTC)]
        keep, _, reasons = select(snaps(times), Policy(keep_monthly=1), UTC)
        self.assertEqual([s.id for s in keep], ["s001"])


class ForgetAndPrune(unittest.TestCase):
    def test_prune_deletes_only_chunks_no_kept_snapshot_uses(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            src = t / "src"
            src.mkdir()
            repo = Repository.init(t / "repo", chunk_size=64 * 1024)
            shared = os.urandom(64 * 1024)
            (src / "shared.bin").write_bytes(shared)
            (src / "old.bin").write_bytes(os.urandom(64 * 1024))
            backup(repo, [str(src)], now=dt.datetime(2026, 1, 1, tzinfo=UTC))
            (src / "old.bin").unlink()
            (src / "new.bin").write_bytes(os.urandom(64 * 1024))
            backup(repo, [str(src)], now=dt.datetime(2026, 1, 2, tzinfo=UTC))
            self.assertEqual(len(list(repo.object_digests())), 3)

            keep, remove, _, dry = forget(repo, Policy(keep_last=1), dry_run=True)
            self.assertIsNone(dry)
            self.assertEqual(len(repo.snapshots()), 2)  # dry run changes nothing

            keep, remove, _, pruned = forget(repo, Policy(keep_last=1))
            self.assertEqual(len(remove), 1)
            self.assertEqual(pruned.removed_objects, 1)  # old.bin only; shared.bin is still used
            self.assertGreater(pruned.freed_bytes, 0)
            self.assertTrue(check(repo, read_data=True).ok)
            self.assertEqual(len(repo.snapshots()), 1)


if __name__ == "__main__":
    unittest.main()
