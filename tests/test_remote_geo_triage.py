"""Tests for remote_geo_triage.py (rule classifier + label write-back).

Run:  python -m unittest tests.test_remote_geo_triage -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from remote_geo_triage import (  # noqa: E402
    LABELS,
    LLM_UNCLEAR_LABEL,
    classify_rules,
    fetch_remote_jobs,
)


class TestClassifyRules(unittest.TestCase):
    def test_germany_named(self):
        self.assertEqual(
            classify_rules("Fully remote within Germany, team meets in Berlin."),
            "germany")

    def test_worldwide_is_germany_eligible(self):
        self.assertEqual(
            classify_rules("We hire from anywhere in the world."), "germany")

    def test_europe_wide(self):
        self.assertEqual(
            classify_rules("Remote role open across Europe (CET overlap required)."),
            "europe")

    def test_us_only(self):
        self.assertEqual(
            classify_rules("You must be authorized to work in the US. US only."),
            "non_eu")

    def test_conflict_stays_unclear(self):
        # Germany named but a US-residency restriction too → let the LLM decide
        self.assertEqual(
            classify_rules("Offices in Germany and the US; candidates must be "
                           "residing in the United States."),
            "unclear")

    def test_no_signal(self):
        self.assertEqual(classify_rules("Great team, exciting stack."), "unclear")

    def test_labels_reach_germany_keyword_filter(self):
        # apply_queue.GERMANY_KEYWORDS matches on 'German' — the Germany label
        # must contain it, the other labels must not
        self.assertIn("German", LABELS["germany"])
        self.assertNotIn("German", LABELS["europe"])
        self.assertNotIn("German", LABELS["non_eu"])
        self.assertNotIn("German", LLM_UNCLEAR_LABEL)


class TestFetchAndWriteBack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, company TEXT, title TEXT,"
            " location TEXT, status TEXT, match_score INTEGER,"
            " raw_jd_text TEXT, translated_jd_text TEXT)")
        rows = [
            ("j1", "Acme", "Backend", "Remote", "scored", 90, "Remote within Germany", None),
            ("j2", "Umbrella", "ML", "Remote", "scored", 85, None, "US only role"),
            ("j3", "Initech", "Dev", "Hamburg", "scored", 88, "irrelevant", None),  # not Remote
            ("j4", "Hooli", "SRE", "Remote", "applied", 92, "anywhere", None),      # not scored
        ]
        self.conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fetch_only_bare_remote_scored(self):
        jobs = fetch_remote_jobs(self.conn)
        self.assertEqual({j["id"] for j in jobs}, {"j1", "j2"})

    def test_llm_unclear_label_leaves_fetch_scope(self):
        # once marked, the job must not be fetched (and thus billed) again
        self.conn.execute(
            "UPDATE jobs SET location=? WHERE id='j2'", (LLM_UNCLEAR_LABEL,))
        jobs = fetch_remote_jobs(self.conn)
        self.assertEqual({j["id"] for j in jobs}, {"j1"})

    def test_write_back_guard_is_idempotent(self):
        self.conn.execute(
            "UPDATE jobs SET location=? WHERE id=? AND location='Remote'",
            (LABELS["germany"], "j1"))
        # second pass: j1 no longer matches location='Remote'
        cur = self.conn.execute(
            "UPDATE jobs SET location=? WHERE id=? AND location='Remote'",
            (LABELS["non_eu"], "j1"))
        self.assertEqual(cur.rowcount, 0)
        loc = self.conn.execute("SELECT location FROM jobs WHERE id='j1'").fetchone()[0]
        self.assertEqual(loc, LABELS["germany"])


if __name__ == "__main__":
    unittest.main()
