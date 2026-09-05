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
    url_is_non_de,
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
        self.assertTrue(is_germany_location("Ditzingen"))      # Trumpf HQ
        self.assertTrue(is_germany_location("Barsinghausen"))

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


class TestIndeedCountrySubdomain(unittest.TestCase):
    """The Indeed country subdomain (es./fr./us.) is a geo signal no location
    string or JD text carries — the motivating case was es.indeed.com jobs
    surfacing into Apply Review despite obviously not being in Germany."""

    def test_non_de_subdomain_flags(self):
        self.assertTrue(url_is_non_de("https://es.indeed.com/viewjob?jk=1153f2"))
        self.assertTrue(url_is_non_de("https://fr.indeed.com/viewjob?jk=abc"))
        self.assertTrue(url_is_non_de("https://us.indeed.com/rc/clk?jk=xyz"))
        self.assertTrue(url_is_non_de("https://uk.indeed.com/viewjob?jk=q"))

    def test_de_subdomain_is_not_non_de(self):
        self.assertFalse(url_is_non_de("https://de.indeed.com/viewjob?jk=743e9c"))

    def test_bare_and_non_country_hosts_are_neutral(self):
        # no country code → fall through to location/JD checks, never veto
        self.assertFalse(url_is_non_de("https://www.indeed.com/viewjob?jk=1"))
        self.assertFalse(url_is_non_de("https://indeed.com/viewjob?jk=1"))
        self.assertFalse(url_is_non_de("https://smartapply.indeed.com/beta/x"))
        self.assertFalse(url_is_non_de("https://boards.greenhouse.io/acme/jobs/1"))
        self.assertFalse(url_is_non_de(""))
        self.assertFalse(url_is_non_de(None))

    def test_url_veto_overrides_bare_location(self):
        # location gives no signal, but the URL alone places it outside Germany
        self.assertTrue(has_non_de_marker("Remote", "https://es.indeed.com/viewjob?jk=1"))
        self.assertTrue(has_non_de_marker("", "https://es.indeed.com/viewjob?jk=1"))
        self.assertFalse(is_germany_location("Frankfurt", "https://es.indeed.com/viewjob?jk=1"))

    def test_de_url_does_not_falsely_promote(self):
        # a de.indeed URL is not non-DE, but it also must not promote a bare
        # non-German location to Germany on its own
        self.assertFalse(is_germany_location("Remote", "https://de.indeed.com/viewjob?jk=1"))
        self.assertTrue(is_germany_location("Hamburg", "https://de.indeed.com/viewjob?jk=1"))


class TestBareCountryAndCityNames(unittest.TestCase):
    """2026-09-05: 516 already-scored rows carried a location the veto list did
    not know — bare "US" alone was 399 of them. They were LLM-scored and then
    sat in the queue's supply as jobs that can never reach a German employer.
    One case per family added that day."""

    def test_bare_us_is_vetoed(self):
        for loc in ("US", "US Remote", "Houston (US)", "Remote, US, California"):
            self.assertTrue(has_non_de_marker(loc), loc)

    def test_us_does_not_eat_german_towns(self):
        """The whole risk of a two-letter token: \b must keep it off these."""
        for loc in ("Neuss", "Husum", "Neuss, Germany", "25813 Husum", "Cottbus"):
            self.assertFalse(has_non_de_marker(loc), loc)
        self.assertTrue(is_germany_location("Neuss, Germany"))
        self.assertTrue(is_germany_location("25813 Husum"))

    def test_gulf_latam_africa_asia_country_names(self):
        for loc in ("United Arab Emirates", "Saudi Arabia", "Doha, Qatar", "Chile",
                    "Colombia", "Uruguay", "Nigeria", "Kenya", "Philippines",
                    "Malaysia", "Thailand", "Bangalore, IN"):
            self.assertTrue(has_non_de_marker(loc), loc)

    def test_european_country_names_the_first_list_missed(self):
        # \bczech\b never matched "Czechia" — that is why it is listed separately
        for loc in ("Czechia", "Croatia", "Estonia", "Latvia", "Lithuania",
                    "Slovenia", "Serbia", "Cyprus", "Malta", "Tirana"):
            self.assertTrue(has_non_de_marker(loc), loc)

    def test_german_language_country_names_from_bundesagentur(self):
        for loc in ("SPANIEN", "NORWEGEN", "Mamer, LUXEMBURG", "M/V Louise Michel, ITALIEN",
                    "Mladá Boleslav, TSCHECHISCHE_REPUBLIK", "Warschau, Polen",
                    "Bukarest (Rumänien)", "Maynooth, Co. Kildare, Irland"):
            self.assertTrue(has_non_de_marker(loc), loc)

    def test_bare_city_names_without_a_country(self):
        for loc in ("Los Angeles", "San Mateo", "Palo Alto", "Mountain View",
                    "Remote / Ottawa", "Remote / Toronto", "Remote / New-York",
                    "Seattle", "Boston", "Austin", "Nashville", "Cairo",
                    "Melbourne", "Kaunas Office", "São Paulo", "Nantes"):
            self.assertTrue(has_non_de_marker(loc), loc)

    def test_us_state_suffix(self):
        for loc in ("Reston, VA", "Nashville, TN", "Washington, DC",
                    "Remote / Santa Clara, CA", "Remote / Mountain View, CA",
                    "Austin, TX; Honolulu, HI; St. Louis, MO; Washington, DC"):
            self.assertTrue(has_non_de_marker(loc), loc)

    def test_state_suffix_does_not_claim_de_or_department_suffixes(self):
        # ", DE" is how several sources write Germany — Delaware is excluded
        # from the state list on purpose. ", IT" is a department, not Italy.
        self.assertTrue(is_germany_location("Walldorf, DE, 69190"))
        self.assertFalse(has_non_de_marker("Bad Homburg, IT"))
        self.assertFalse(has_non_de_marker("Offenburg, Development"))

    def test_ambiguous_pools_still_flow_to_the_scorer(self):
        # absence of a marker is not evidence of Germany — these stay scorable
        for loc in ("Remote", "Remote — EU", "Remote - EMEA", "Europe",
                    "Homeoffice", "Wuppertal", "Erlangen", "Schwäbisch Hall"):
            self.assertFalse(has_non_de_marker(loc), loc)


if __name__ == "__main__":
    unittest.main()
