"""Tests for set_job_ats — the ats_scan → jobs table write path.

Focus: a scan that finds no better apply link must not erase an apply_url a
source already supplied (wearedevelopers' detail API pre-populates it).

Run:  python -m unittest discover tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import (create_application_snapshot, expire_gone_scored_jobs,  # noqa: E402
                      init_db, set_job_ats)


def _make_job(conn, job_id, *, apply_url=None, status="scored", ats=None):
    conn.execute(
        "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, "
        "  fetched_at, status, apply_url, ats) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job_id, "Acme", f"Title {job_id}", f"https://wad.example/{job_id}",
         "wearedevelopers", "x" * 600, "2026-07-13T12:00:00", status, apply_url, ats),
    )
    conn.commit()


class SetJobAtsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _apply_url(self, job_id):
        return self.conn.execute(
            "SELECT apply_url FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["apply_url"]

    def test_none_apply_url_keeps_existing(self):
        # WAD stored the real external target; a fruitless scan (apply_url=None)
        # must still refresh the ats verdict but keep the known-good link
        _make_job(self.conn, "j1", apply_url="https://workingnomads.com/jobs/x")
        ok = set_job_ats(self.conn, "j1", "unknown-external", apply_url=None)
        self.assertTrue(ok)
        self.assertEqual(self._apply_url("j1"), "https://workingnomads.com/jobs/x")
        self.assertEqual(
            self.conn.execute("SELECT ats FROM jobs WHERE id = ?", ("j1",)).fetchone()["ats"],
            "unknown-external",
        )

    def test_new_apply_url_overwrites(self):
        # a scan that resolves a better (e.g. real ATS) link still wins
        _make_job(self.conn, "j2", apply_url="https://workingnomads.com/jobs/x")
        set_job_ats(self.conn, "j2", "greenhouse",
                    apply_url="https://boards.greenhouse.io/acme/jobs/1")
        self.assertEqual(self._apply_url("j2"), "https://boards.greenhouse.io/acme/jobs/1")

    def test_missing_row_returns_false(self):
        self.assertFalse(set_job_ats(self.conn, "nope", "gone", apply_url=None))


class ExpireGoneScoredJobsTest(unittest.TestCase):
    """ats='gone' must leave the scored pool (build_queue drops it, but the
    dashboard filters on status only) — except when a draft is in flight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _status(self, job_id):
        return self.conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]

    def test_scored_gone_job_is_expired(self):
        _make_job(self.conn, "g1", status="scored", ats="gone")
        self.assertEqual(expire_gone_scored_jobs(self.conn), 1)
        self.assertEqual(self._status("g1"), "expired")

    def test_live_and_transient_jobs_untouched(self):
        _make_job(self.conn, "ok", status="scored", ats="greenhouse")
        _make_job(self.conn, "fe", status="scored", ats="fetch-error")  # transient
        _make_job(self.conn, "nil", status="scored", ats=None)
        self.assertEqual(expire_gone_scored_jobs(self.conn), 0)
        for jid in ("ok", "fe", "nil"):
            self.assertEqual(self._status(jid), "scored")

    def test_non_scored_gone_job_untouched(self):
        # already applied to a since-removed posting — never rewrite a tracked status
        _make_job(self.conn, "ap", status="applied", ats="gone")
        self.assertEqual(expire_gone_scored_jobs(self.conn), 0)
        self.assertEqual(self._status("ap"), "applied")

    def test_gone_job_with_in_flight_draft_is_kept(self):
        _make_job(self.conn, "dr", status="scored", ats="gone")
        create_application_snapshot(self.conn, "dr", status="draft", tier=2,
                                    apply_url="https://wad.example/dr")
        self.assertEqual(expire_gone_scored_jobs(self.conn), 0)
        self.assertEqual(self._status("dr"), "scored")


if __name__ == "__main__":
    unittest.main()
