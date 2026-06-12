"""Smoke tests for pages/1_Apply_Review.py via streamlit.testing.AppTest.

Needs streamlit (present in the pipeline container); skips cleanly on the
host venv. Fixture data is fictional (Max Mustermann policy).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from streamlit.testing.v1 import AppTest
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

from utils.db import create_application_snapshot, init_db  # noqa: E402
from utils.snapshot_io import approve_snapshot, report_result  # noqa: E402

PAGE = str(Path(__file__).resolve().parents[1] / "pages" / "1_Apply_Review.py")

PAYLOAD = {"actions": [{"selector": "#fn", "kind": "text", "label": "Vorname",
                        "action": "fill", "value": "Max",
                        "source": "profile:first_name", "needs_review": False}],
           "unfilled": [{"label": "Referral", "selector": "#ref",
                         "reason": "no-deterministic-match", "required": False}],
           "never_fill_skipped": []}


@unittest.skipUnless(HAS_STREAMLIT, "streamlit not installed on this host")
class ReviewPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["DB_PATH"] = self.db_path
        self.conn = init_db(self.db_path)
        for jid in ("job-a", "job-b"):
            self.conn.execute(
                "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
                " fetched_at, status, match_score, fit_grade)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'scored', 80, 'A')",
                (jid, "Mustermann GmbH", "Backend Engineer",
                 f"https://example.com/{jid}", "test", f"jd {jid}",
                 "2026-06-12T08:00:00"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("DB_PATH", None)

    def _draft(self, job_id, tier=2, **fields):
        defaults = {"status": "draft", "tier": tier, "channel": "company-form",
                    "apply_url": f"https://example.com/{job_id}/apply",
                    "form_payload": PAYLOAD,
                    "cover_letter": "Dear team, ...",
                    "custom_qa": [{"question": "Why us?", "answer": "Because."}],
                    "verifier_report": {"pass": False, "llm_checked": True,
                                        "issues": [{"where": "cover_letter",
                                                    "issue": "unsupported claim",
                                                    "severity": "high"}]}}
        defaults.update(fields)
        return create_application_snapshot(self.conn, job_id, **defaults)

    def _run(self):
        at = AppTest.from_file(PAGE, default_timeout=10)
        at.run()
        self.assertFalse(at.exception, at.exception)
        return at

    def test_renders_empty_queue(self):
        at = self._run()
        self.assertTrue(at.info)  # the 'no drafts' notice

    def test_renders_drafts_with_verifier_issues_and_metrics(self):
        self._draft("job-a", tier=2)
        self._draft("job-b", tier=3, verifier_report={})
        at = self._run()
        self.assertEqual(at.metric[0].value, "2")  # drafts metric
        self.assertTrue(any("unsupported claim" in str(e.value) for e in at.error))
        body = "".join(str(c.value) for c in at.code)
        self.assertIn("Max", body)  # answer-sheet copy block

    def test_approve_button_writes_approved_at(self):
        sid = self._draft("job-a", tier=2)
        at = self._run()
        at.button(key=f"approve_{sid}").click().run()
        self.assertFalse(at.exception, at.exception)
        row = self.conn.execute(
            "SELECT status, approved_at FROM application_snapshots WHERE id=?",
            (sid,)).fetchone()
        self.assertEqual(row["status"], "approved")
        self.assertTrue(row["approved_at"])

    def test_tier3_draft_has_no_approve_button(self):
        sid = self._draft("job-b", tier=3)
        at = self._run()
        keys = {b.key for b in at.button}
        self.assertNotIn(f"approve_{sid}", keys)
        self.assertIn(f"abandon_{sid}", keys)

    def test_abandon_button_releases_job(self):
        sid = self._draft("job-a", tier=2)
        at = self._run()
        at.button(key=f"abandon_{sid}").click().run()
        row = self.conn.execute(
            "SELECT status FROM application_snapshots WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["status"], "abandoned")

    def test_last_failure_shown_on_regenerated_draft(self):
        first = self._draft("job-a", tier=2)
        approve_snapshot(self.conn, first)
        report_result(self.conn, first, "failed", note="drift: 3/10 unfillable")
        self._draft("job-a", tier=2)  # the regenerated draft
        at = self._run()
        self.assertTrue(any("drift: 3/10 unfillable" in str(e.value)
                            for e in at.error))


if __name__ == "__main__":
    unittest.main()
