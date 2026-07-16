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
    WORLDWIDE_LOCATIONS,
    classify_rules,
    fetch_de_candidates,
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

    def test_marketing_worldwide_is_not_a_hiring_signal(self):
        # real mislabels (07-09..15 review queue): "organizations worldwide" /
        # "millions of people worldwide" in US-only JDs → Remote — Germany
        self.assertEqual(
            classify_rules("Impact the security infrastructure of major "
                           "organizations worldwide. US Pay Range $160,000."),
            "unclear")
        self.assertEqual(
            classify_rules("our mission is to build a worldwide community "
                           "connected by healthy habits"),
            "unclear")

    def test_hiring_context_worldwide_still_counts(self):
        self.assertEqual(
            classify_rules("We are remote-first and hire worldwide."), "germany")
        self.assertEqual(
            classify_rules("This role is remote worldwide."), "germany")

    def test_from_anywhere_with_region_qualifier_is_a_restriction(self):
        # real mislabel: Taxgpt "Work from anywhere across US, Canada or Mexico"
        self.assertEqual(
            classify_rules("Remote-first: Work from anywhere across US, "
                           "Canada or Mexico."),
            "unclear")

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
            ("j3", "Initech", "Dev", "Hamburg", "scored", 88, "irrelevant", None),  # keyword match
            ("j4", "Hooli", "SRE", "Remote", "applied", 92, "anywhere", None),      # not scored
            ("j5", "Piper", "Go dev", "Anywhere in the World", "scored", 90, "great team", None),
            ("j6", "Vandelay", "QA", "Remote / New York", "scored", 88, "eng org", None),
            ("j7", "Sirius", "Data", "Dresden (DE)", "scored", 88, "eng org", None),
            ("j8", "Globex", "SRE", "Municipality of Madrid, Spain", "scored", 88, "x", None),
            # un-scored: triage now runs before phase2_scorer and must see these
            ("j9", "Wonka", "Rust dev", "Remote", "un-scored", None, "EU-wide, CET overlap", None),
            ("j10", "Stark", "Embedded", "Karlsruhe", "un-scored", None, "x", None),
        ]
        self.conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fetch_remote_pool_includes_variants(self):
        jobs = fetch_remote_jobs(self.conn)
        self.assertEqual({j["id"] for j in jobs}, {"j1", "j2", "j5", "j6", "j9"})

    def test_llm_unclear_label_leaves_fetch_scope(self):
        # once marked, the job must not be fetched (and thus billed) again
        self.conn.execute(
            "UPDATE jobs SET location=? WHERE id='j2'", (LLM_UNCLEAR_LABEL,))
        jobs = fetch_remote_jobs(self.conn)
        self.assertEqual({j["id"] for j in jobs}, {"j1", "j5", "j6", "j9"})

    def test_worldwide_locations_are_lowercase(self):
        # membership test in main() lowers the location first
        for loc in WORLDWIDE_LOCATIONS:
            self.assertEqual(loc, loc.lower())

    def test_de_candidates_pool(self):
        # keyword-matched (j3 Hamburg), Remote-ish (j1/j2/j6), worldwide (j5)
        # and post-pipeline states (j4 applied) are out; un-scored (j10) is in
        jobs = fetch_de_candidates(self.conn)
        self.assertEqual({j["id"] for j in jobs}, {"j7", "j8", "j10"})

    def test_write_back_guard_is_idempotent(self):
        self.conn.execute(
            "UPDATE jobs SET location=? WHERE id=? AND location=?",
            (LABELS["germany"], "j1", "Remote"))
        # second pass carries the stale old location — must not overwrite
        cur = self.conn.execute(
            "UPDATE jobs SET location=? WHERE id=? AND location=?",
            (LABELS["non_eu"], "j1", "Remote"))
        self.assertEqual(cur.rowcount, 0)
        loc = self.conn.execute("SELECT location FROM jobs WHERE id='j1'").fetchone()[0]
        self.assertEqual(loc, LABELS["germany"])

    def test_pass0_relabel_reaches_keyword_filter(self):
        # the appended label must contain 'German' so apply_queue picks it up
        new_loc = "Dresden (DE), Germany"
        self.conn.execute(
            "UPDATE jobs SET location=? WHERE id=? AND location=?",
            (new_loc, "j7", "Dresden (DE)"))
        self.assertIn("German", new_loc)
        # and the job leaves the pass-0 pool on the next run
        jobs = fetch_de_candidates(self.conn)
        self.assertEqual({j["id"] for j in jobs}, {"j8", "j10"})


if __name__ == "__main__":
    unittest.main()
