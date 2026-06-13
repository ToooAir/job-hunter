"""Tests for utils/snapshot_io.py — snapshot lifecycle, check-out/check-in.

Pure sqlite on a temp file; runs on host and in the container.
Fixture data is fictional (Max Mustermann policy).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import (  # noqa: E402
    create_application_snapshot,
    get_in_flight_snapshots,
    init_db,
)
from utils.snapshot_io import (  # noqa: E402
    abandon_snapshot,
    approve_snapshot,
    fetch_work,
    last_failure,
    report_result,
)

PAYLOAD = {"actions": [{"selector": "#fn", "kind": "text", "label": "Vorname",
                        "action": "fill", "value": "Max",
                        "source": "profile:first_name", "needs_review": False}],
           "unfilled": [], "never_fill_skipped": []}


class SnapshotIOTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "test.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _job(self, job_id="job-1", company="Mustermann GmbH"):
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'scored')",
            (job_id, company, "Backend Engineer",
             f"https://example.com/{job_id}", "test", f"jd text {job_id}",
             "2026-06-12T08:00:00"),
        )
        self.conn.commit()
        return job_id

    def _snapshot(self, job_id, **fields):
        defaults = {"status": "draft", "tier": 2, "channel": "company-form",
                    "apply_url": "https://example.com/apply",
                    "form_payload": PAYLOAD}
        defaults.update(fields)
        return create_application_snapshot(self.conn, job_id, **defaults)

    # ── check-out ──────────────────────────────────────────────────────────

    def test_fetch_work_returns_only_requested_status(self):
        sid = self._snapshot(self._job("a"))
        self._snapshot(self._job("b"))  # stays draft
        approve_snapshot(self.conn, sid)
        work = fetch_work(self.conn)
        self.assertEqual([w["id"] for w in work], [sid])

    def test_fetch_work_decodes_json_and_joins_job(self):
        sid = self._snapshot(self._job())
        approve_snapshot(self.conn, sid)
        snap = fetch_work(self.conn)[0]
        self.assertEqual(snap["form_payload"]["actions"][0]["value"], "Max")
        self.assertEqual(snap["job"]["company"], "Mustermann GmbH")
        self.assertEqual(snap["job"]["status"], "scored")

    # ── review decisions ───────────────────────────────────────────────────

    def test_approve_sets_approved_at_only_from_draft(self):
        sid = self._snapshot(self._job())
        approve_snapshot(self.conn, sid)
        snap = fetch_work(self.conn)[0]
        self.assertEqual(snap["status"], "approved")
        self.assertTrue(snap["approved_at"])
        with self.assertRaises(ValueError):  # double approval
            approve_snapshot(self.conn, sid)

    def test_abandon_records_reason_and_releases_job(self):
        job_id = self._job()
        sid = self._snapshot(job_id)
        abandon_snapshot(self.conn, sid, "stale posting")
        self.assertEqual(get_in_flight_snapshots(self.conn), [])
        row = self.conn.execute(
            "SELECT status, notes FROM application_snapshots WHERE id=?",
            (sid,)).fetchone()
        self.assertEqual(row["status"], "abandoned")
        self.assertIn("stale posting", row["notes"])

    # ── check-in ───────────────────────────────────────────────────────────

    def test_submitted_flips_job_to_applied(self):
        job_id = self._job()
        sid = self._snapshot(job_id)
        approve_snapshot(self.conn, sid)
        report_result(self.conn, sid, "submitted",
                      screenshot_path="data/screenshots/1.png")
        snap = self.conn.execute(
            "SELECT * FROM application_snapshots WHERE id=?", (sid,)).fetchone()
        self.assertEqual(snap["status"], "submitted")
        self.assertTrue(snap["submitted_at"])
        self.assertEqual(snap["submitted_by"], "agent")
        self.assertEqual(snap["screenshot_path"], "data/screenshots/1.png")
        job = self.conn.execute(
            "SELECT status, applied_at, follow_up_at FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        self.assertEqual(job["status"], "applied")
        self.assertTrue(job["applied_at"])
        self.assertTrue(job["follow_up_at"])

    def test_tier3_watch_submits_straight_from_draft(self):
        job_id = self._job()
        sid = self._snapshot(job_id, tier=3)
        report_result(self.conn, sid, "submitted", submitted_by="human")
        snap = self.conn.execute(
            "SELECT status, submitted_by FROM application_snapshots WHERE id=?",
            (sid,)).fetchone()
        self.assertEqual(snap["status"], "submitted")
        self.assertEqual(snap["submitted_by"], "human")

    def test_failed_requires_reason_and_releases_job(self):
        job_id = self._job()
        sid = self._snapshot(job_id)
        approve_snapshot(self.conn, sid)
        with self.assertRaises(ValueError):
            report_result(self.conn, sid, "failed")
        report_result(self.conn, sid, "failed", note="drift: 3/10 unfillable")
        self.assertEqual(get_in_flight_snapshots(self.conn), [])  # re-queues
        job = self.conn.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(job["status"], "scored")  # job untouched

    def test_prepared_keeps_snapshot_approved(self):
        sid = self._snapshot(self._job())
        approve_snapshot(self.conn, sid)
        report_result(self.conn, sid, "prepared",
                      screenshot_path="data/screenshots/2.png")
        snap = self.conn.execute(
            "SELECT status, notes, screenshot_path FROM application_snapshots"
            " WHERE id=?", (sid,)).fetchone()
        self.assertEqual(snap["status"], "approved")  # human may still submit
        self.assertIn("prepared", snap["notes"])
        self.assertEqual(snap["screenshot_path"], "data/screenshots/2.png")

    def test_illegal_transition_raises(self):
        sid = self._snapshot(self._job())
        approve_snapshot(self.conn, sid)
        report_result(self.conn, sid, "submitted")
        with self.assertRaises(ValueError):
            report_result(self.conn, sid, "failed", note="too late")

    def test_unknown_outcome_raises(self):
        sid = self._snapshot(self._job())
        with self.assertRaises(ValueError):
            report_result(self.conn, sid, "teleported")

    def test_notes_accumulate_with_timestamps(self):
        job_id = self._job()
        sid = self._snapshot(job_id, notes="tier2: cover letter present")
        approve_snapshot(self.conn, sid)
        report_result(self.conn, sid, "failed", note="captcha appeared live")
        row = self.conn.execute(
            "SELECT notes FROM application_snapshots WHERE id=?", (sid,)).fetchone()
        self.assertIn("tier2: cover letter present", row["notes"])
        self.assertIn("failed: captcha appeared live", row["notes"])

    # ── same-company dedup on submit (watchlist #2) ─────────────────────────

    def test_submit_abandons_sibling_drafts_same_company(self):
        # Two channels for the same company (suffix differs); applying via one
        # must abandon the other so the manual path can't double-submit.
        sid = self._snapshot(self._job("a", "Acme GmbH"))
        other = self._snapshot(self._job("b", "Acme AG"))
        approve_snapshot(self.conn, sid)
        freed = report_result(self.conn, sid, "submitted")
        self.assertEqual(freed, [other])
        row = self.conn.execute(
            "SELECT status, notes FROM application_snapshots WHERE id=?",
            (other,)).fetchone()
        self.assertEqual(row["status"], "abandoned")
        self.assertIn(f"#{sid}", row["notes"])

    def test_submit_leaves_other_companies_alone(self):
        sid = self._snapshot(self._job("a", "Acme GmbH"))
        keep = self._snapshot(self._job("b", "Globex SE"))
        approve_snapshot(self.conn, sid)
        freed = report_result(self.conn, sid, "submitted")
        self.assertEqual(freed, [])
        row = self.conn.execute(
            "SELECT status FROM application_snapshots WHERE id=?",
            (keep,)).fetchone()
        self.assertEqual(row["status"], "draft")

    def test_non_submit_outcomes_abandon_nothing(self):
        sid = self._snapshot(self._job("a", "Acme GmbH"))
        self._snapshot(self._job("b", "Acme AG"))
        approve_snapshot(self.conn, sid)
        self.assertEqual(report_result(self.conn, sid, "failed", note="x"), [])

    # ── review-page support ────────────────────────────────────────────────

    def test_last_failure_returns_newest_failed(self):
        job_id = self._job()
        first = self._snapshot(job_id)
        approve_snapshot(self.conn, first)
        report_result(self.conn, first, "failed", note="selector drift")
        second = self._snapshot(job_id)  # regenerated draft
        approve_snapshot(self.conn, second)
        report_result(self.conn, second, "failed", note="page gone")
        info = last_failure(self.conn, job_id)
        self.assertEqual(info["id"], second)
        self.assertIn("page gone", info["notes"])
        self.assertIsNone(last_failure(self.conn, "no-such-job"))


if __name__ == "__main__":
    unittest.main()
