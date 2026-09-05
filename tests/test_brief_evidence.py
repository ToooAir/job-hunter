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

from utils.db import (  # noqa: E402
    init_db, create_application_snapshot, add_interview_record,
    get_all_interview_records,
)
from phase2_scorer import (  # noqa: E402
    _submission_evidence, _past_interview_block, PAST_INTERVIEW_CHARS,
)


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

    def test_custom_qa_list_shape_is_the_production_one(self):
        """snapshot_io.append_custom_qa writes a list of {question, answer};
        the reader used to assume a dict and raised AttributeError, taking the
        whole brief down for every job that had answered a form question."""
        create_application_snapshot(
            self.conn, "j1", status="submitted", submitted_by="human",
            submitted_at="2026-06-13 00:12", channel="greenhouse",
            cover_letter="Dear Hiring Team,",
            custom_qa=[{"question": "Why us?", "answer": "Because Y.",
                        "source": "on-demand", "asked_at": "2026-06-13T00:10:00"}],
        )
        out = _submission_evidence(self.conn, self._job(), "en")
        self.assertIn("**Q:** Why us?", out)
        self.assertIn("**A:** Because Y.", out)
        self.assertNotIn("on-demand", out)

    def test_zh_labels(self):
        create_application_snapshot(
            self.conn, "j1", status="submitted", cover_letter="本文。")
        out = _submission_evidence(self.conn, self._job(), "zh")
        self.assertIn("你當時實際提交的內容", out)


class PastInterviewBlockTest(unittest.TestCase):
    """The brief used to see only the JD and the KB. The funnel's hole is
    9 first rounds → 0 second rounds, so it now also sees what the candidate
    actually gets asked."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = init_db(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _rec(self, job_id, date, **kw):
        rec = {"job_id": job_id, "round": "interview_1", "interview_date": date,
               "interviewer": "Erika Mustermann", "format": "video",
               "questions": "Why Germany? Tell us about a hard project.",
               "self_rating": 4, "impressions": "Small team, flat structure.",
               "created_at": f"{date}T12:00:00"}
        rec.update(kw)
        add_interview_record(self.conn, rec)

    def test_no_records_yields_no_block(self):
        self.assertEqual(_past_interview_block(self.conn, "en"), "")

    def test_records_without_content_are_skipped(self):
        self._rec("j1", "2026-06-01", questions=None, impressions=None)
        self.assertEqual(_past_interview_block(self.conn, "en"), "")

    def test_block_carries_questions_rating_and_date(self):
        self._rec("j1", "2026-06-01")
        block = _past_interview_block(self.conn, "en")
        self.assertIn("Why Germany?", block)
        self.assertIn("2026-06-01", block)
        self.assertIn("4/5", block)
        self.assertIn("Small team", block)

    def test_interviewer_name_never_enters_the_prompt(self):
        self._rec("j1", "2026-06-01")
        self.assertNotIn("Erika", _past_interview_block(self.conn, "en"))
        self.assertNotIn("Mustermann", _past_interview_block(self.conn, "en"))

    def test_current_job_is_excluded(self):
        self._rec("j1", "2026-06-01", questions="Question from this very company")
        self._rec("j2", "2026-05-01", questions="Question from somewhere else")
        block = _past_interview_block(self.conn, "en", exclude_job_id="j1")
        self.assertNotIn("this very company", block)
        self.assertIn("somewhere else", block)

    def test_second_rounds_are_not_first_rounds(self):
        self._rec("j1", "2026-06-01", round="interview_2", questions="Round two question")
        self.assertEqual(_past_interview_block(self.conn, "en"), "")

    def test_newest_first_and_capped(self):
        for i in range(1, 9):
            self._rec(f"j{i}", f"2026-0{i}-01", questions="Q" * 4000, impressions="I" * 4000)
        block = _past_interview_block(self.conn, "en")
        self.assertLessEqual(len(block), PAST_INTERVIEW_CHARS)
        # newest date present, oldest pushed out by the budget
        self.assertIn("2026-08-01", block)
        self.assertIn("…", block)   # trimmed, and the cut is visible

    def test_zh_labels(self):
        self._rec("j1", "2026-06-01")
        block = _past_interview_block(self.conn, "zh")
        self.assertIn("問題:", block)
        self.assertIn("自評:", block)

    def test_get_all_interview_records_orders_newest_first(self):
        self._rec("j1", "2026-05-01")
        self._rec("j2", "2026-07-01")
        rows = get_all_interview_records(self.conn)
        self.assertEqual([r["interview_date"] for r in rows], ["2026-07-01", "2026-05-01"])


if __name__ == "__main__":
    unittest.main()
