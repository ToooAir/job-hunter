"""Daily application counts must use the same clock that stamps applied_at.

Regression: applied_at is written in naive container-local time (Europe/Berlin)
while the dashboard derived its Mon→today window from UTC. Between local
midnight and 02:00 CEST the two clocks sit on different dates, so applications
booked in that window were filtered out entirely — "Applied Today" showed
yesterday's number and the bar chart had no bucket for them.
"""

import os
import tempfile
import unittest
from datetime import datetime

from utils.db import (DRAFT_STALE_DAYS, create_application_snapshot,
                      daily_applied_counts, draft_age_days, draft_stock,
                      init_db, today_local_iso, week_ago_local_iso)


def make_applied(conn, job_id, applied_at, status="applied"):
    conn.execute(
        "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, fetched_at,"
        " status, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, "Acme", f"Title {job_id}", f"https://example.com/{job_id}",
         "heise", "x" * 600, "2026-08-20T10:00:00", status, applied_at),
    )
    conn.commit()


class DailyAppliedCountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = init_db(os.path.join(self.tmp, "jobs.db"))

    def tearDown(self):
        self.conn.close()

    def test_after_local_midnight_counts_toward_the_new_day(self):
        # Friday 2026-08-28, 00:23 local — UTC is still 2026-08-27T22:23
        now = datetime(2026, 8, 28, 0, 23, 5)
        make_applied(self.conn, "night", "2026-08-28T00:23:05")
        make_applied(self.conn, "yesterday", "2026-08-27T15:00:00")

        days = daily_applied_counts(self.conn, now=now)

        self.assertEqual(days[-1], {"day": "2026-08-28", "cnt": 1})
        self.assertEqual(days[0]["day"], "2026-08-24")  # Monday of that week
        self.assertEqual(len(days), 5)
        self.assertEqual(sum(d["cnt"] for d in days), 2)

    def test_zero_fills_days_without_applications(self):
        now = datetime(2026, 8, 28, 9, 0, 0)
        make_applied(self.conn, "mon", "2026-08-24T11:00:00")

        days = daily_applied_counts(self.conn, now=now)

        self.assertEqual([d["cnt"] for d in days], [1, 0, 0, 0, 0])

    def test_ignores_other_weeks_and_non_pipeline_statuses(self):
        now = datetime(2026, 8, 28, 9, 0, 0)
        make_applied(self.conn, "lastweek", "2026-08-21T11:00:00")
        make_applied(self.conn, "expired", "2026-08-26T11:00:00", status="expired")
        make_applied(self.conn, "rejected", "2026-08-26T11:00:00", status="rejected")

        days = daily_applied_counts(self.conn, now=now)

        self.assertEqual(sum(d["cnt"] for d in days), 1)

    def test_monday_window_starts_at_that_monday(self):
        now = datetime(2026, 8, 24, 0, 30, 0)  # Monday, just past local midnight
        make_applied(self.conn, "mon", "2026-08-24T00:29:00")

        days = daily_applied_counts(self.conn, now=now)

        self.assertEqual(days, [{"day": "2026-08-24", "cnt": 1}])


class LocalClockHelpersTest(unittest.TestCase):
    def test_today_and_week_ago_use_the_injected_local_now(self):
        now = datetime(2026, 8, 28, 0, 23, 5)
        self.assertEqual(today_local_iso(now), "2026-08-28")
        self.assertEqual(week_ago_local_iso(now), "2026-08-21T00:23:05")


class DraftAgeingTest(unittest.TestCase):
    """Drafts rot — the posting expires while the draft waits for the human.
    created_at is stamped on the local clock, so the age must be read on it
    too; a UTC now() would shift every age by the CEST offset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(os.path.join(self.tmp.name, "t.db")))
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status) VALUES ('j1','Acme','T','u','heise','x','2026-08-01','scored')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _draft(self, created_at):
        sid = create_application_snapshot(self.conn, "j1", status="draft")
        self.conn.execute("UPDATE application_snapshots SET created_at=? WHERE id=?",
                          (created_at, sid))
        self.conn.commit()
        return sid

    def test_age_is_whole_days_on_the_local_clock(self):
        now = datetime(2026, 9, 5, 9, 0, 0)
        self.assertEqual(draft_age_days("2026-08-12T08:08:00", now), 24)
        self.assertEqual(draft_age_days("2026-09-05T08:00:00", now), 0)

    def test_future_timestamp_clamps_to_zero(self):
        self.assertEqual(draft_age_days("2026-09-06T00:00:00",
                                        datetime(2026, 9, 5, 9, 0)), 0)

    def test_unparseable_or_missing_returns_none_rather_than_guessing(self):
        self.assertIsNone(draft_age_days(None))
        self.assertIsNone(draft_age_days(""))
        self.assertIsNone(draft_age_days("not-a-date"))

    def test_stock_counts_waiting_oldest_and_stale(self):
        now = datetime(2026, 9, 5, 12, 0, 0)
        self._draft("2026-09-05T08:00:00")                      # 0d
        self._draft("2026-08-29T08:00:00")                      # 7d — not stale
        self._draft("2026-08-12T08:00:00")                      # 24d — stale
        stock = draft_stock(self.conn, now)
        self.assertEqual(stock["count"], 3)
        self.assertEqual(stock["oldest_days"], 24)
        self.assertEqual(stock["stale"], 1)                     # strictly older than 7

    def test_stock_ignores_non_draft_snapshots(self):
        create_application_snapshot(self.conn, "j1", status="submitted")
        stock = draft_stock(self.conn, datetime(2026, 9, 5, 12, 0))
        self.assertEqual(stock, {"count": 0, "oldest_days": None, "stale": 0})

    def test_stale_threshold_is_the_shared_constant(self):
        self.assertEqual(DRAFT_STALE_DAYS, 7)

if __name__ == "__main__":
    unittest.main()
