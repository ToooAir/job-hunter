"""Tests for utils.company_alias.extract_company_aliases + its ingest hook.

Governing principle: precision over recall. A missed alias only keeps the
status quo; a wrong alias poisons the /email-match nomination list. So the
positives are real markers and the negatives guard against generic-token and
company-self noise.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.company_alias import extract_company_aliases  # noqa: E402
from utils.db import init_db, upsert_job  # noqa: E402


class ExtractCompanyAliasesTest(unittest.TestCase):
    def _aliases(self, company, jd):
        return [a.strip() for a in extract_company_aliases(company, jd).split(",")
                if a.strip()]

    # ── positives: real markers ──────────────────────────────────────────
    def test_bzw_and_trademark_real_case(self):
        # the U-Glow / prelytics case that motivated the feature
        jd = ("Bei U-Glow bzw. prelytics® entwickeln wir benutzerfreundliche"
              " Softwarelösungen für komplexe Aufgaben.")
        self.assertIn("prelytics", self._aliases("U-Glow GmbH", jd))

    def test_trademark_alone(self):
        jd = "We are Globex, and our flagship product Initech™ powers logistics."
        self.assertIn("Initech", self._aliases("Globex SE", jd))

    def test_trading_as(self):
        jd = "Acme Holding GmbH, trading as Rocketworks, is hiring engineers."
        self.assertIn("Rocketworks", self._aliases("Acme Holding GmbH", jd))

    def test_formerly_known_as(self):
        jd = "Nimbus AG (formerly known as Cirrus) builds weather models."
        self.assertIn("Cirrus", self._aliases("Nimbus AG", jd))

    def test_slash_gated_on_company_left_side(self):
        jd = "Willkommen bei U-Glow / prelytics, dem Analytics-Spezialisten."
        self.assertIn("prelytics", self._aliases("U-Glow GmbH", jd))

    # ── negatives: precision guards ──────────────────────────────────────
    def test_no_markers_yields_empty(self):
        jd = "We build reliable backend systems in Python. Join our team today."
        self.assertEqual(extract_company_aliases("Acme GmbH", jd), "")

    def test_company_self_is_not_an_alias(self):
        # a trademark on the company's own name must not echo it back
        jd = "Acme® is the market leader in robotics."
        self.assertEqual(self._aliases("Acme GmbH", jd), [])

    def test_generic_token_after_connective_dropped(self):
        jd = "Globex, formerly the market leader, now trading as software."
        self.assertNotIn("software", [a.lower() for a in
                                      self._aliases("Globex SE", jd)])
        self.assertNotIn("the", [a.lower() for a in
                                 self._aliases("Globex SE", jd)])

    def test_unrelated_slash_ignored(self):
        # left side of the slash is not the company → not an alias (avoids
        # paths, "and/or", unrelated pairs)
        jd = "Send a CV and/or portfolio. Visit https://x/careers/apply now."
        self.assertEqual(self._aliases("Acme GmbH", jd), [])

    def test_capped_at_three(self):
        jd = ("Acme, trading as Aone, bzw. Btwo® formerly Cthree also known as"
              " Dfour aka Efive builds things.")
        self.assertLessEqual(len(self._aliases("Acme GmbH", jd)), 3)

    def test_empty_inputs(self):
        self.assertEqual(extract_company_aliases("", "x bzw. y"), "")
        self.assertEqual(extract_company_aliases("Acme", ""), "")


class UpsertPopulatesAliasesTest(unittest.TestCase):
    """The ingest choke point (upsert_job) mines and stores the alias."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "jobs.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _record(self, **over):
        rec = {
            "id": "j1", "company": "U-Glow GmbH", "title": "Python Dev",
            "url": "https://x/j1", "source": "test",
            "raw_jd_text": "Bei U-Glow bzw. prelytics® entwickeln wir " + "x" * 600,
            "fetched_at": "2026-08-01T08:00:00", "status": "un-scored",
        }
        rec.update(over)
        return rec

    def _aliases_of(self, jid):
        return self.conn.execute(
            "SELECT company_aliases FROM jobs WHERE id = ?", (jid,)
        ).fetchone()["company_aliases"]

    def test_upsert_mines_alias_from_jd(self):
        self.assertTrue(upsert_job(self.conn, self._record()))
        self.assertIn("prelytics", self._aliases_of("j1"))

    def test_caller_supplied_alias_is_not_overwritten(self):
        rec = self._record(id="j2", url="https://x/j2",
                           company_aliases="hand-set")
        self.assertTrue(upsert_job(self.conn, rec))
        self.assertEqual(self._aliases_of("j2"), "hand-set")


if __name__ == "__main__":
    unittest.main()
