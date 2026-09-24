"""Smoke tests for pages/1_Apply_Review.py via streamlit.testing.AppTest.

Needs streamlit (present in the pipeline container); skips cleanly on the
host venv. Fixture data is fictional (Max Mustermann policy).
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from streamlit.testing.v1 import AppTest
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

from utils.db import (  # noqa: E402
    DRAFT_STALE_DAYS, create_application_snapshot, draft_age_days,
    draft_stock, init_db,
)

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

    def _age(self, snapshot_id, days):
        """Backdate a draft — create_application_snapshot always stamps now."""
        ts = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        self.conn.execute(
            "UPDATE application_snapshots SET created_at = ? WHERE id = ?",
            (ts, snapshot_id))
        self.conn.commit()

    def test_stale_drafts_are_flagged_and_sorted_first(self):
        """A draft rots: the posting expires while it waits. The friction/score
        order used to bury old drafts indefinitely — 2026-09-05 the oldest was
        24 days old and three pointed at jobs already expired."""
        fresh = self._draft("job-a", tier=2)
        stale = self._draft("job-b", tier=2)
        self._age(fresh, 1)
        self._age(stale, DRAFT_STALE_DAYS + 13)
        at = self._run()
        self.assertTrue(at.warning)                    # the stale-drafts hint
        self.assertIn(str(DRAFT_STALE_DAYS + 13), str(at.warning[0].value)
                      + "".join(e.label for e in at.expander))
        labels = [e.label for e in at.expander]
        self.assertTrue(labels[0].startswith("🔴"), labels)
        self.assertNotIn("🔴", labels[1])

    def _scored_job(self, jid, score, title, company):
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status, match_score, fit_grade)"
            " VALUES (?, ?, ?, ?, 'test', ?, '2026-06-12T08:00:00', 'scored', ?, ?)",
            (jid, company, title, f"https://example.com/{jid}", f"jd {jid}",
             score, "A" if score >= 78 else "B"))
        self.conn.commit()

    def _order_of(self, *titles):
        """Positions of each title among the rendered cards, in queue order."""
        labels = [e.label for e in self._run().expander]
        return [next(i for i, lb in enumerate(labels) if t in lb) for t in titles]

    def test_waiting_time_outweighs_a_small_score_gap(self):
        """The old key ignored age entirely below the stale line: a draft one
        day from rotting sat under a same-day draft scoring two points higher.
        Age now ramps instead of jumping, so four days of waiting beat ten
        points of match."""
        self._scored_job("job-old", 70, "Aged Engineer", "Alpha GmbH")
        self._scored_job("job-new", 80, "Fresh Engineer", "Beta GmbH")
        self._age(self._draft("job-old", tier=2), DRAFT_STALE_DAYS)
        self._age(self._draft("job-new", tier=2), 0)
        aged, fresh = self._order_of("Aged Engineer", "Fresh Engineer")
        self.assertLess(aged, fresh)

    def test_score_still_leads_at_equal_age(self):
        """...but the ramp must not swamp the match score, or the queue just
        turns into a different single-factor sort."""
        self._scored_job("job-low", 70, "Weak Match", "Alpha GmbH")
        self._scored_job("job-high", 80, "Strong Match", "Beta GmbH")
        self._age(self._draft("job-low", tier=2), 2)
        self._age(self._draft("job-high", tier=2), 2)
        high, low = self._order_of("Strong Match", "Weak Match")
        self.assertLess(high, low)

    def test_friction_no_longer_outranks_a_much_better_match(self):
        """Friction used to be the first key, so a mediocre Tier 2 draft buried
        every Tier 3 — including the best matches in the queue. It is now a
        cost in score points, big enough to break near-ties and no bigger."""
        self._scored_job("job-board", 92, "Top Match", "Alpha GmbH")
        self._scored_job("job-easy", 74, "Easy Form", "Beta GmbH")
        self._age(self._draft("job-board", tier=3, channel="external-board"), 0)
        self._age(self._draft("job-easy", tier=2), 0)
        top, easy = self._order_of("Top Match", "Easy Form")
        self.assertLess(top, easy)

    def test_suspicious_liveness_sinks_below_its_live_twin(self):
        """Same work, less payoff: the sweep already doubts the posting is
        live, so it should not hold a slot above an identical live draft."""
        self._scored_job("job-live", 80, "Live Posting", "Alpha GmbH")
        self._scored_job("job-doubt", 80, "Doubtful Posting", "Beta GmbH")
        # the doubtful one is inserted first: with the old key it tied on
        # friction and score, and insert order alone put it on top
        self._age(self._draft("job-doubt", tier=2, liveness="suspicious"), 5)
        self._age(self._draft("job-live", tier=2, liveness="live"), 5)
        live, doubt = self._order_of("Live Posting", "Doubtful Posting")
        self.assertLess(live, doubt)

    def test_oldest_draft_metric(self):
        sid = self._draft("job-a")
        self._age(sid, 9)
        at = self._run()
        self.assertEqual(at.metric[1].value, "9 天")

    def test_renders_empty_queue(self):
        at = self._run()
        self.assertTrue(at.info)  # the 'no drafts' notice

    def test_renders_drafts_with_verifier_issues_and_metrics(self):
        self._draft("job-a", tier=2)
        sid_b = self._draft("job-b", tier=3, verifier_report={})
        at = self._run()
        self.assertEqual(at.metric[0].value, "2")  # drafts metric
        self.assertTrue(any("unsupported claim" in str(e.value) for e in at.error))
        # an unflagged letter collapses by default (the extension's 📄 button
        # serves the same stored text); the toggle opens it for hand-copying
        self.assertFalse([a for a in at.text_area if a.key == f"cl_{sid_b}"])
        at.toggle(key=f"cl_show_{sid_b}").set_value(True).run()
        self.assertIn("Dear team", at.text_area(key=f"cl_{sid_b}").value)

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

    def test_tier3_letter_is_editable_and_the_edit_survives_save(self):
        # Tier 3 has no fillable fields, but it is exactly where the human
        # sends the letter by hand — a wrong line there (a junk company name)
        # has to be fixable before it is copied or downloaded.
        sid = self._draft("job-b", tier=3)
        at = self._run()
        self.assertIn(f"submit_{sid}", {b.key for b in at.button})
        at.toggle(key=f"cl_show_{sid}").set_value(True).run()
        at.text_area(key=f"cl_{sid}").set_value("Fixed letter.").run()
        at.button(key=f"save_{sid}").click().run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(self.conn.execute(
            "SELECT cover_letter FROM application_snapshots WHERE id=?",
            (sid,)).fetchone()["cover_letter"], "Fixed letter.")

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

    def test_cl_flag_appears_once_and_letter_stays_closed(self):
        # De-dup: a high cover-letter issue renders exactly once — next to the
        # letter, where it can be fixed — not again as a generic blocking
        # issue. The alarm lives OUTSIDE the toggle, so the letter itself
        # stays closed even when flagged (opening is always an explicit act).
        sid = self._draft("job-a", tier=2)
        at = self._run()
        hits = [e for e in at.error if "unsupported claim" in str(e.value)]
        self.assertEqual(len(hits), 1)
        self.assertFalse(any("需處理的阻擋問題" in str(e.value) for e in at.error))
        self.assertFalse(at.toggle(key=f"cl_show_{sid}").value)

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

    def test_email_only_draft_hands_over_address_subject_and_jd(self):
        # No form and no page: the card has to carry what the human would
        # otherwise have to dig out — who to write to, a subject, and the JD.
        self._draft("job-a", tier=3, channel="email-only",
                    apply_url="mailto:jobs@example.com", verifier_report={})
        at = self._run()
        blocks = [str(c.value) for c in at.code]
        self.assertIn("jobs@example.com", blocks)
        self.assertIn("Bewerbung als Backend Engineer", blocks)
        self.assertNotIn("jd job-a", blocks)          # JD is read only on demand
        at.toggle(key="jd_1").set_value(True).run()
        self.assertIn("jd job-a", [str(c.value) for c in at.code])

    def _book(self, job_id, status, applied_at):
        self.conn.execute("UPDATE jobs SET status = ?, applied_at = ? WHERE id = ?",
                          (status, applied_at, job_id))
        self.conn.commit()

    def test_company_dup_warning_names_the_prior_outcome(self):
        # "already applied here" is ambiguous on its own — a rejection and an
        # open thread are different re-apply decisions, so the banner must say
        # which one the prior application ended in.
        self._book("job-b", "rejected", "2026-07-21T16:52:53")
        self._draft("job-a", tier=2)
        at = self._run()
        banner = "".join(str(w.value) for w in at.warning)
        self.assertIn("同公司已投過", banner)
        self.assertIn("2026-07-21", banner)
        self.assertIn("已被拒", banner)

    def test_company_dup_history_lists_every_prior_application(self):
        # The most recent application alone can hide the rejection that
        # matters, so every booked row at this company is listed.
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status, match_score, fit_grade)"
            " VALUES ('job-c', 'Mustermann GmbH', 'Data Engineer',"
            " 'https://example.com/job-c', 'test', 'jd c', '2026-06-12T08:00:00',"
            " 'scored', 80, 'A')")
        self.conn.commit()
        self._book("job-b", "rejected", "2026-05-01T09:00:00")
        self._book("job-c", "applied", "2026-07-21T16:52:53")
        self._draft("job-a", tier=2)
        at = self._run()
        history = "".join(str(c.value) for c in at.caption)
        self.assertIn("2 筆", history)
        self.assertIn("已被拒", history)     # the older rejection is not dropped
        self.assertIn("等回覆中", history)

    def test_same_job_dup_error_names_the_outcome(self):
        self._book("job-a", "ghosted", "2026-07-21T16:52:53")
        self._draft("job-a", tier=2)
        at = self._run()
        banner = "".join(str(e.value) for e in at.error)
        self.assertIn("重複投遞", banner)
        self.assertIn("無回音", banner)


if __name__ == "__main__":
    unittest.main()
