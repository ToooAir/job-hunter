"""Tests for utils/resume_stats.py — the résumé-effectiveness funnel math.

Run:  python -m unittest tests.test_resume_stats -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import init_db  # noqa: E402
from utils.resume_stats import effectiveness, quick_rejects  # noqa: E402


def add(conn, job_id, *, status, peak_stage, grade="A", score=85,
        source="heise", applied_at="2026-06-15T10:00:00"):
    conn.execute(
        "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, fetched_at, "
        "  fit_grade, match_score, status, peak_stage, applied_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, f"Co-{job_id}", f"T {job_id}", f"https://e.com/{job_id}", source,
         "x" * 600, "2026-06-14T10:00:00", grade, score, status, peak_stage, applied_at),
    )
    conn.commit()


class EffectivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_two_filter_funnel(self):
        add(self.conn, "iv", status="rejected", peak_stage="interview_1")  # interviewed then rejected
        add(self.conn, "rej", status="rejected", peak_stage="applied")     # read, declined
        add(self.conn, "gh", status="ghosted", peak_stage="applied")       # no reply
        add(self.conn, "pend", status="applied", peak_stage="applied")     # still open
        o = effectiveness(self.conn)["overall"]
        self.assertEqual(o["applied"], 4)
        self.assertEqual(o["interview"], 1)     # peak reached interview
        self.assertEqual(o["ghosted"], 1)
        self.assertEqual(o["pending"], 1)
        self.assertEqual(o["responded"], 2)     # iv + rej
        # response_rate over decided (responded+ghosted=3), excludes pending
        self.assertEqual(o["response_rate"], round(100 * 2 / 3, 1))
        # interview_rate over all applied
        self.assertEqual(o["interview_rate"], 25.0)

    def test_offer_counts_as_interview_reached(self):
        add(self.conn, "off", status="offer", peak_stage="offer")
        o = effectiveness(self.conn)["overall"]
        self.assertEqual(o["interview"], 1)
        self.assertEqual(o["offer"], 1)

    def test_ignores_unapplied_jobs(self):
        add(self.conn, "applied", status="applied", peak_stage="applied")
        # a scored-but-not-applied job must not enter the funnel
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, fetched_at, "
            "  fit_grade, match_score, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("scored", "X", "T", "u", "heise", "x" * 600, "2026-06-14T10:00:00",
             "A", 90, "scored"),
        )
        self.conn.commit()
        self.assertEqual(effectiveness(self.conn)["overall"]["applied"], 1)

    def test_by_grade_split(self):
        add(self.conn, "a1", status="rejected", peak_stage="interview_1", grade="A")
        add(self.conn, "a2", status="ghosted", peak_stage="applied", grade="A")
        add(self.conn, "b1", status="rejected", peak_stage="applied", grade="B")
        g = effectiveness(self.conn)["by_grade"]
        self.assertEqual(g["A"]["applied"], 2)
        self.assertEqual(g["A"]["interview"], 1)
        self.assertEqual(g["A"]["interview_rate"], 50.0)
        self.assertEqual(g["B"]["interview"], 0)
        self.assertEqual(g["B"]["interview_rate"], 0.0)

    def test_by_source_split(self):
        add(self.conn, "w1", status="rejected", peak_stage="interview_1", source="wttj")
        add(self.conn, "j1", status="ghosted", peak_stage="applied", source="jobware")
        s = effectiveness(self.conn)["by_source"]
        self.assertEqual(s["wttj"]["interview"], 1)
        self.assertEqual(s["jobware"]["interview"], 0)

    def test_since_isolates_cohort(self):
        add(self.conn, "old", status="rejected", peak_stage="interview_1",
            applied_at="2026-05-01T10:00:00")
        add(self.conn, "new", status="ghosted", peak_stage="applied",
            applied_at="2026-07-02T10:00:00")
        o = effectiveness(self.conn, since="2026-07-01")["overall"]
        self.assertEqual(o["applied"], 1)
        self.assertEqual(o["ghosted"], 1)
        self.assertEqual(o["interview"], 0)

    def test_empty_db_no_zero_division(self):
        o = effectiveness(self.conn)["overall"]
        self.assertEqual(o["applied"], 0)
        self.assertIsNone(o["response_rate"])
        self.assertIsNone(o["interview_rate"])


if __name__ == "__main__":
    unittest.main()


class QuickRejectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _rej(self, job_id, applied, booked, source="heise", lang="en_required"):
        add(self.conn, job_id, status="rejected", peak_stage="applied",
            source=source, applied_at=f"{applied}T10:00:00")
        self.conn.execute(
            "UPDATE jobs SET notes = ?, jd_language_req = ? WHERE id = ?",
            (f"\n[{booked}] rejected booked from pasted email (extension)", lang, job_id))
        self.conn.commit()

    def test_days_median_and_quick_share(self):
        self._rej("a", "2026-08-01", "2026-08-03")            # 2 days
        self._rej("b", "2026-08-01", "2026-08-08")            # 7 days (quick)
        self._rej("c", "2026-08-01", "2026-08-20")            # 19 days
        add(self.conn, "untimed", status="rejected", peak_stage="applied")  # no note
        s = quick_rejects(self.conn)
        self.assertEqual(s["overall"]["n"], 3)
        self.assertEqual(s["untimed"], 1)
        self.assertEqual(s["overall"]["median_days"], 7)
        self.assertEqual(s["overall"]["quick"], 2)
        self.assertEqual(s["overall"]["quick_share"], round(100 * 2 / 3, 1))

    def test_grouped_by_source_and_language(self):
        self._rej("a", "2026-08-01", "2026-08-02", source="bundesagentur", lang="de_plus")
        self._rej("b", "2026-08-01", "2026-08-30", source="wttj", lang="en_required")
        s = quick_rejects(self.conn)
        self.assertEqual(s["by_source"]["bundesagentur"]["quick"], 1)
        self.assertEqual(s["by_source"]["wttj"]["quick"], 0)
        self.assertEqual(s["by_lang"]["de_plus"]["n"], 1)

    def test_since_filters_on_applied_at(self):
        self._rej("old", "2026-06-01", "2026-06-03")
        self._rej("new", "2026-08-01", "2026-08-03")
        self.assertEqual(quick_rejects(self.conn, since="2026-07-01")["overall"]["n"], 1)

