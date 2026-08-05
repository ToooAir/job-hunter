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
    def test_bzw_gated_real_case(self):
        # the U-Glow / prelytics case that motivated the feature: 'bzw.' is
        # trusted only because its left side IS the company
        jd = ("Bei U-Glow bzw. prelytics® entwickeln wir benutzerfreundliche"
              " Softwarelösungen für komplexe Aufgaben.")
        self.assertIn("prelytics", self._aliases("U-Glow GmbH", jd))

    def test_bzw_second_brand(self):
        # the anchored marker works past the trademark form too
        jd = "Willkommen bei Globex bzw. Initech, dem Marktführer."
        self.assertIn("Initech", self._aliases("Globex SE", jd))

    # ── negatives: precision guards ──────────────────────────────────────
    def test_no_markers_yields_empty(self):
        jd = "We build reliable backend systems in Python. Join our team today."
        self.assertEqual(extract_company_aliases("Acme GmbH", jd), "")

    def test_company_self_is_not_an_alias(self):
        # "Acme bzw. Acme GmbH" must not echo the company back as its own alias
        jd = "Acme bzw. Acme GmbH is the market leader in robotics."
        self.assertEqual(self._aliases("Acme GmbH", jd), [])

    def test_generic_token_after_marker_dropped(self):
        # even anchored, a generic / stop-word right side is not a brand
        self.assertEqual(self._aliases("Acme GmbH", "Acme bzw. software teams"), [])
        self.assertEqual(self._aliases("Acme GmbH", "Acme bzw. im Team"), [])

    def test_ungated_connectives_dropped(self):
        # real regression: 'formerly / trading as / a.k.a.' fire on third-party
        # vendor mentions, so they are not trusted. A storage reseller's JD:
        # "Everpure (formerly Pure Storage)" must NOT alias the employer.
        jd = ("Structured Communication Systems partners with Dell, Everpure"
              " (formerly Pure Storage), Rubrik and more.")
        self.assertEqual(
            extract_company_aliases("Structured Communication Systems, Inc.", jd),
            "")
        self.assertEqual(
            extract_company_aliases("Acme GmbH",
                                    "Acme, trading as Rocketworks, hires."), "")

    def test_german_bzw_conjunction_not_captured(self):
        # real regression: 'bzw.' is an everyday German conjunction, so an
        # UNANCHORED one (left side is not the company) must never yield an
        # alias — this used to emit 'praktische Erfahrung im'
        jd = ("Wir suchen einen Entwickler bzw. praktische Erfahrung im"
              " Bereich Cloud ist erwünscht.")
        self.assertEqual(extract_company_aliases("E.v. Bonn", jd), "")

    def test_unanchored_trademark_ignored(self):
        # real regression: a ® / ™ on some other firm's product in the JD
        # (about™, CD®) is not the employer's brand
        self.assertEqual(
            extract_company_aliases("HomeToGo GmbH", "Learn more about™ us."), "")
        self.assertEqual(
            extract_company_aliases("Getspecialfasteners", "We ship CD® media."),
            "")

    def test_slash_noise_ignored(self):
        # the "X / Y" rule was dropped: tech stacks and URL paths collide with
        # it. Real regressions: 'CI/CD' emitted 'CD' ('ci' hid inside
        # 'getspeCIalfasteners'), 'hometogo.com/about' emitted 'about'.
        self.assertEqual(
            extract_company_aliases("Getspecialfasteners",
                                    "Experience with CI/CD and REST/GraphQL."), "")
        self.assertEqual(
            extract_company_aliases("HomeToGo GmbH",
                                    "Read more at hometogo.com/about today."), "")
        self.assertEqual(extract_company_aliases(
            "Acme GmbH", "Send a CV and/or portfolio to https://x/apply."), "")

    def test_bzw_short_substring_left_not_trusted(self):
        # a 2-char left token that happens to sit inside the company name must
        # not open the gate
        self.assertEqual(
            extract_company_aliases("Getspecialfasteners", "ci bzw. nonsense"), "")

    def test_capped_at_three(self):
        jd = ("Acme bzw. Aone. Acme bzw. Btwo. Acme bzw. Cthree."
              " Acme bzw. Dfour. Acme bzw. Efive.")
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
