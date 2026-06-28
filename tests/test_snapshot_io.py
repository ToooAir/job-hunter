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
    edit_snapshot,
    fetch_work,
    mark_submitted,
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

    def test_fetch_work_returns_only_draft_status(self):
        # different companies so submitting one doesn't sibling-abandon the other
        sid = self._snapshot(self._job("a", "Acme GmbH"))
        other = self._snapshot(self._job("b", "Globex SE"))
        mark_submitted(self.conn, other)  # leaves the draft queue
        work = fetch_work(self.conn)
        self.assertEqual([w["id"] for w in work], [sid])

    def test_fetch_work_decodes_json_and_joins_job(self):
        self._snapshot(self._job())
        snap = fetch_work(self.conn)[0]
        self.assertEqual(snap["form_payload"]["actions"][0]["value"], "Max")
        self.assertEqual(snap["job"]["company"], "Mustermann GmbH")
        self.assertEqual(snap["job"]["status"], "scored")

    # ── review decisions ───────────────────────────────────────────────────

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

    def test_mark_submitted_flips_job_to_applied(self):
        job_id = self._job()
        sid = self._snapshot(job_id)
        mark_submitted(self.conn, sid)
        snap = self.conn.execute(
            "SELECT * FROM application_snapshots WHERE id=?", (sid,)).fetchone()
        self.assertEqual(snap["status"], "submitted")
        self.assertTrue(snap["submitted_at"])
        self.assertEqual(snap["submitted_by"], "human")
        job = self.conn.execute(
            "SELECT status, applied_at, follow_up_at FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        self.assertEqual(job["status"], "applied")
        self.assertTrue(job["applied_at"])
        self.assertTrue(job["follow_up_at"])

    def test_mark_submitted_only_from_draft(self):
        sid = self._snapshot(self._job())
        mark_submitted(self.conn, sid)
        with self.assertRaises(ValueError):  # already submitted
            mark_submitted(self.conn, sid)

    def test_notes_accumulate_with_timestamps(self):
        job_id = self._job()
        sid = self._snapshot(job_id, notes="tier2: cover letter present")
        mark_submitted(self.conn, sid, note="marked submitted in review")
        row = self.conn.execute(
            "SELECT notes FROM application_snapshots WHERE id=?", (sid,)).fetchone()
        self.assertIn("tier2: cover letter present", row["notes"])
        self.assertIn("marked submitted in review", row["notes"])

    # ── same-company dedup on submit (watchlist #2) ─────────────────────────

    def test_submit_abandons_sibling_drafts_same_company(self):
        # Two channels for the same company (suffix differs); applying via one
        # must abandon the other so the manual path can't double-submit.
        sid = self._snapshot(self._job("a", "Acme GmbH"))
        other = self._snapshot(self._job("b", "Acme AG"))
        freed = mark_submitted(self.conn, sid)
        self.assertEqual(freed, [other])
        row = self.conn.execute(
            "SELECT status, notes FROM application_snapshots WHERE id=?",
            (other,)).fetchone()
        self.assertEqual(row["status"], "abandoned")
        self.assertIn(f"#{sid}", row["notes"])

    def test_submit_leaves_other_companies_alone(self):
        sid = self._snapshot(self._job("a", "Acme GmbH"))
        keep = self._snapshot(self._job("b", "Globex SE"))
        freed = mark_submitted(self.conn, sid)
        self.assertEqual(freed, [])
        row = self.conn.execute(
            "SELECT status FROM application_snapshots WHERE id=?",
            (keep,)).fetchone()
        self.assertEqual(row["status"], "draft")

    # ── in-place edits before submission ────────────────────────────────────

    def _editable_snap(self, job_id):
        payload = {"actions": [
            {"selector": "#src", "kind": "text", "label": "How did you hear",
             "action": "fill", "value": "LinkedIN", "source": "llm",
             "needs_review": True},
            {"selector": "#cl", "kind": "text", "label": "Anschreiben",
             "action": "fill", "value": "Dear Acme team", "source": "cover_letter",
             "needs_review": True}],
            "unfilled": [], "never_fill_skipped": []}
        return self._snapshot(job_id, form_payload=payload,
                              cover_letter="Dear Acme team")

    def test_edit_field_value_persists(self):
        sid = self._editable_snap(self._job())
        changed = edit_snapshot(self.conn, sid,
                                action_values={"#src": "Job posting"})
        self.assertEqual(changed, ["How did you hear"])
        snap = self.conn.execute(
            "SELECT status, form_payload FROM application_snapshots WHERE id=?",
            (sid,)).fetchone()
        self.assertEqual(snap["status"], "draft")  # editing does not submit
        import json as _json
        actions = _json.loads(snap["form_payload"])["actions"]
        self.assertEqual(actions[0]["value"], "Job posting")

    def test_edit_cover_letter_syncs_bound_action(self):
        sid = self._editable_snap(self._job())
        changed = edit_snapshot(self.conn, sid, cover_letter="Dear Substain team")
        self.assertEqual(changed, ["cover letter"])
        import json as _json
        row = self.conn.execute(
            "SELECT cover_letter, form_payload FROM application_snapshots"
            " WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["cover_letter"], "Dear Substain team")
        cl_action = [a for a in _json.loads(row["form_payload"])["actions"]
                     if a["source"] == "cover_letter"][0]
        self.assertEqual(cl_action["value"], "Dear Substain team")  # kept in sync

    def test_edit_noop_when_unchanged(self):
        sid = self._editable_snap(self._job())
        self.assertEqual(
            edit_snapshot(self.conn, sid, cover_letter="Dear Acme team",
                          action_values={"#src": "LinkedIN"}),
            [])

    def test_edit_rejected_after_submission(self):
        sid = self._editable_snap(self._job())
        mark_submitted(self.conn, sid)
        with self.assertRaises(ValueError):
            edit_snapshot(self.conn, sid, cover_letter="too late")


if __name__ == "__main__":
    unittest.main()
