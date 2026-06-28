"""Tests for the deterministic 'What You Submitted' brief section.

Container-only (phase2_scorer imports openai). The section is pulled verbatim
from the submitted snapshot — never via the LLM — so these tests pin the
fidelity contract: what shows up is exactly what was sent.

Fixture data is fictional (Max Mustermann policy).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import init_db, create_application_snapshot  # noqa: E402
from phase2_scorer import _submission_evidence  # noqa: E402


class SubmissionEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = init_db(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _job(self, **kw):
        base = {"id": "j1", "cover_letter_draft": None}
        base.update(kw)
        return base

    def test_no_snapshot_no_draft_returns_empty(self):
        self.assertEqual(_submission_evidence(self.conn, self._job(), "en"), "")

    def test_submitted_snapshot_is_verbatim(self):
        create_application_snapshot(
            self.conn, "j1", status="submitted", submitted_by="agent",
            submitted_at="2026-06-13 00:12", channel="generic-form",
            cover_letter="Dear Hiring Team, I built X.",
            custom_qa={"Why us?": "Because Y."},
        )
        out = _submission_evidence(self.conn, self._job(), "en")
        self.assertIn("What You Actually Submitted", out)
        self.assertIn("Dear Hiring Team, I built X.", out)
        self.assertIn("submitted version", out)
        self.assertIn("Why us?", out)
        self.assertIn("Because Y.", out)
        self.assertIn("agent", out)

    def test_draft_fallback_is_flagged(self):
        # No submitted snapshot — falls back to the draft with a caveat.
        out = _submission_evidence(
            self.conn, self._job(cover_letter_draft="Draft body."), "en")
        self.assertIn("Draft body.", out)
        self.assertIn("may differ", out)

    def test_submitted_beats_draft(self):
        create_application_snapshot(
            self.conn, "j1", status="submitted",
            cover_letter="Sent body.")
        out = _submission_evidence(
            self.conn, self._job(cover_letter_draft="Draft body."), "en")
        self.assertIn("Sent body.", out)
        self.assertNotIn("Draft body.", out)

    def test_non_submitted_snapshot_ignored(self):
        # A draft snapshot must not count as evidence of submission.
        create_application_snapshot(
            self.conn, "j1", status="draft", cover_letter="Unsent.")
        self.assertEqual(_submission_evidence(self.conn, self._job(), "en"), "")

    def test_zh_labels(self):
        create_application_snapshot(
            self.conn, "j1", status="submitted", cover_letter="本文。")
        out = _submission_evidence(self.conn, self._job(), "zh")
        self.assertIn("你當時實際提交的內容", out)


if __name__ == "__main__":
    unittest.main()
