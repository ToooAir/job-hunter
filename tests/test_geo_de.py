"""Tests for utils.geo_de.is_germany_location (precision matcher).

Run:  python -m unittest tests.test_geo_de -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.geo_de import (  # noqa: E402
    DE_POSTAL_SENTINEL,
    GERMANY_PATTERNS,
    has_non_de_marker,
    is_germany_location,
)


class TestIsGermanyLocation(unittest.TestCase):
    # ── real gap cases from the DB that must now match ──
    def test_second_tier_city(self):
        self.assertTrue(is_germany_location("Nuremberg"))
        self.assertTrue(is_germany_location("Karlsruhe"))
        self.assertTrue(is_germany_location("Darmstadt"))

    def test_de_suffix(self):
        self.assertTrue(is_germany_location("Dresden (DE)"))
        self.assertTrue(is_germany_location("Rastede (DE)"))

    def test_de_comma_token(self):
        self.assertTrue(is_germany_location("Walldorf, DE, 69190"))

    def test_postal_code_forms(self):
        self.assertTrue(is_germany_location("54595 Prüm"))
        self.assertTrue(is_germany_location("89077 Ulm, 82024 Taufkirchen"))
        self.assertTrue(is_germany_location("85570 Markt Schwaben"))

    def test_bundesweit(self):
        self.assertTrue(is_germany_location("Bundesweit"))

    def test_hq_small_towns(self):
        self.assertTrue(is_germany_location("Renningen"))
        self.assertTrue(is_germany_location("Walldorf"))

    # ── veto: pattern/postal hit inside a non-German location ──
    def test_non_de_country_vetoes_city_hit(self):
        self.assertFalse(is_germany_location("Halle, Belgium"))
        self.assertFalse(is_germany_location("Munster, United States of America"))

    def test_us_zip_with_conflict_city(self):
        self.assertFalse(is_germany_location("94104\n\t\t\t\t \n\t\t\t\tSan Francisco"))

    def test_us_dot_abbreviation_at_end(self):
        # postal hit alone would say Germany; the trailing "U.S." must veto
        self.assertFalse(is_germany_location("12345 Springfield, U.S."))

    def test_austrian_and_swiss(self):
        self.assertFalse(is_germany_location("Wien oder Remote"))
        self.assertFalse(is_germany_location("Innsbruck (Österreich)"))
        self.assertFalse(is_germany_location("Zürich, Switzerland"))

    def test_foreign_zip_without_country_name(self):
        # Spanish/French zips are 5-digit too — the city must veto the postal
        # hit even when no country is named ("28046 Madrid" was relabeled
        # "28046 Madrid, Germany" and drafted before the city veto existed)
        self.assertFalse(is_germany_location("28046 Madrid"))
        self.assertFalse(is_germany_location("08018 Barcelona"))
        self.assertFalse(is_germany_location("75008 Paris"))
        # and the poisoned appended form stays vetoed (mixed string)
        self.assertFalse(is_germany_location("28046 Madrid, Germany"))

    def test_lisbonne_contains_bonn(self):
        self.assertFalse(is_germany_location("Remote / Lisbonne"))

    def test_french_canton_de_is_not_de_token(self):
        self.assertFalse(is_germany_location("Canton de Marseille-12, France"))

    # ── no signal at all → not Germany ──
    def test_no_signal(self):
        self.assertFalse(is_germany_location("Schlieren"))   # Swiss town, bare
        self.assertFalse(is_germany_location("Dublin"))
        self.assertFalse(is_germany_location("0 km"))
        self.assertFalse(is_germany_location(""))
        self.assertFalse(is_germany_location(None))

    def test_remote_labels_are_not_germany(self):
        # the triage passes own these — pass 0 must never touch them
        self.assertFalse(is_germany_location("Remote"))
        self.assertFalse(is_germany_location("Remote — EU"))
        self.assertFalse(is_germany_location("Remote — non-EU"))
        self.assertFalse(is_germany_location("Remote — unclear"))

    def test_sentinel_is_not_matched_as_substring(self):
        self.assertIn(DE_POSTAL_SENTINEL, GERMANY_PATTERNS)
        self.assertFalse(is_germany_location("__de_postal__ somewhere"))


class TestHasNonDeMarker(unittest.TestCase):
    """Scoring-veto predicate: only outright foreign markers count."""

    def test_foreign_locations_marked(self):
        self.assertTrue(has_non_de_marker("Municipality of Madrid, Spain"))
        self.assertTrue(has_non_de_marker("New York, United States of America"))
        self.assertTrue(has_non_de_marker("Charing Cross, United Kingdom"))
        self.assertTrue(has_non_de_marker("Wien oder Remote"))

    def test_ambiguous_locations_still_scored(self):
        # absence of a marker is not evidence of Germany — these must score
        self.assertFalse(has_non_de_marker("Remote"))
        self.assertFalse(has_non_de_marker("Schlieren"))   # bare Swiss town
        self.assertFalse(has_non_de_marker(""))
        self.assertFalse(has_non_de_marker(None))

    def test_triage_labels(self):
        # non-EU is excluded via phase2_scorer.geo_excluded's exact match,
        # not via this marker; EU stays scored for the human-review ranking
        self.assertFalse(has_non_de_marker("Remote — EU"))
        self.assertFalse(has_non_de_marker("Remote — non-EU"))
        self.assertFalse(has_non_de_marker("Remote — Germany"))

    def test_german_locations_never_marked(self):
        self.assertFalse(has_non_de_marker("Hamburg"))
        self.assertFalse(has_non_de_marker("Dresden (DE)"))
        self.assertFalse(has_non_de_marker("54595 Prüm"))


if __name__ == "__main__":
    unittest.main()
