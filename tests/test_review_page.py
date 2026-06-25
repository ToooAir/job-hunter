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
        # generated content (cover letter, Q&A) is shown by default for copying;
        # deterministic profile fills collapse behind the auto-fill / sheet toggles.
        body = "".join(str(c.value) for c in at.code)
        self.assertIn("Dear team", body)

    def test_mark_submitted_books_job_applied(self):
        sid = self._draft("job-a", tier=2)
        at = self._run()
        at.button(key=f"submit_{sid}").click().run()
        self.assertFalse(at.exception, at.exception)
        row = self.conn.execute(
            "SELECT status, submitted_at, submitted_by FROM application_snapshots"
            " WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["status"], "submitted")
        self.assertTrue(row["submitted_at"])
        self.assertEqual(row["submitted_by"], "human")
        job = self.conn.execute(
            "SELECT status FROM jobs WHERE id=?", ("job-a",)).fetchone()
        self.assertEqual(job["status"], "applied")

    def test_tier3_draft_has_no_save_button_but_can_mark_submitted(self):
        # Tier 3 is the read-only copy-paste path: no in-place editing, but the
        # human still books it submitted after applying manually.
        sid = self._draft("job-b", tier=3)
        at = self._run()
        keys = {b.key for b in at.button}
        self.assertNotIn(f"save_{sid}", keys)
        self.assertIn(f"submit_{sid}", keys)
        self.assertIn(f"abandon_{sid}", keys)

    def test_abandon_button_releases_job(self):
        sid = self._draft("job-a", tier=2)
        at = self._run()
        at.button(key=f"abandon_{sid}").click().run()
        row = self.conn.execute(
            "SELECT status FROM application_snapshots WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["status"], "abandoned")

    def test_low_severity_only_is_not_alarming(self):
        # watchlist #13: a draft whose only verifier issues are low-severity
        # must not render as a red error (collapsed into a muted expander).
        self._draft("job-a", tier=2, verifier_report={
            "pass": False, "llm_checked": True,
            "issues": [{"where": "cover_letter", "issue": "slightly verbose",
                        "severity": "low"}]})
        at = self._run()
        self.assertFalse(any("slightly verbose" in str(e.value) for e in at.error))

    def test_fabrication_flag_shown_on_cover_letter_tab(self):
        # C: a fabrication issue is surfaced next to the letter (cl_flagged
        # header on the Cover Letter tab), not only in the generic verifier
        # block — so a flagged claim is read in context before approving.
        self._draft("job-a", tier=2, verifier_report={
            "pass": False, "llm_checked": True,
            "issues": [{"where": "cover_letter", "kind": "fabrication",
                        "issue": "claims an award not in the background",
                        "severity": "high"}]})
        at = self._run()
        self.assertTrue(any("被標記的疑慮" in str(e.value) for e in at.error))

    def test_friction_badge_renders_for_mixed_queue(self):
        self._draft("job-a", tier=2)
        self._draft("job-b", tier=3, verifier_report={})
        at = self._run()  # renders without exception; badges in expander labels
        self.assertFalse(at.exception, at.exception)

    def test_document_slots_surface_as_a_notice(self):
        # watchlist #7: extra upload fields (Zeugnisse, CL-PDF) the human must
        # attach by hand are flagged up front, not buried as jargon reasons.
        self._draft("job-a", tier=2, form_payload={
            "actions": [], "never_fill_skipped": [],
            "unfilled": [
                {"label": "Zeugnisse", "selector": "#z",
                 "reason": "attachment-unmapped", "required": True},
                {"label": "Anschreiben", "selector": "#cl",
                 "reason": "cover-letter-upload", "required": False},
            ]})
        at = self._run()
        notice = "".join(str(w.value) for w in at.warning)
        self.assertIn("Zeugnisse", notice)
        self.assertIn("Anschreiben", notice)


if __name__ == "__main__":
    unittest.main()
