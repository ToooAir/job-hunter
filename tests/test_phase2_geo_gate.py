"""Tests for phase2_scorer.geo_excluded (pre-flight scoring veto).

Needs the LLM SDK stack installed (phase2_scorer imports openai at module
level) — run inside the container:
    docker exec job-hunter-pipeline-1 python3 -m unittest tests.test_phase2_geo_gate -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase2_scorer import geo_excluded  # noqa: E402


class TestGeoExcluded(unittest.TestCase):
    def test_outright_foreign_is_excluded(self):
        self.assertTrue(geo_excluded("Municipality of Madrid, Spain"))
        self.assertTrue(geo_excluded("San Francisco, United States of America"))
        self.assertTrue(geo_excluded("Paris, France"))

    def test_triage_non_eu_verdict_is_excluded(self):
        # remote_geo_triage runs before this stage and may have relabelled
        self.assertTrue(geo_excluded("Remote — non-EU"))

    def test_germany_and_remote_pools_are_scored(self):
        self.assertFalse(geo_excluded("Hamburg"))
        self.assertFalse(geo_excluded("Dresden (DE), Germany"))
        self.assertFalse(geo_excluded("Remote"))
        self.assertFalse(geo_excluded("Remote — Germany"))
        self.assertFalse(geo_excluded("Anywhere in the World"))

    def test_remote_eu_is_scored_for_review_ranking(self):
        # the manual Remote — EU review sorts by match_score — keep scoring it
        self.assertFalse(geo_excluded("Remote — EU"))

    def test_empty_location_is_scored(self):
        self.assertFalse(geo_excluded(""))
        self.assertFalse(geo_excluded(None))

    def test_indeed_country_subdomain_excludes_bare_location(self):
        # es.indeed.com is a Spanish-market posting no location string reveals;
        # a bare "Remote"/empty location must still be excluded from scoring
        self.assertTrue(geo_excluded("Remote", "https://es.indeed.com/viewjob?jk=1"))
        self.assertTrue(geo_excluded("", "https://fr.indeed.com/viewjob?jk=1"))
        # de.indeed.com and non-Indeed hosts do not exclude on the URL alone
        self.assertFalse(geo_excluded("Remote", "https://de.indeed.com/viewjob?jk=1"))
        self.assertFalse(geo_excluded("Hamburg", "https://www.indeed.com/viewjob?jk=1"))


if __name__ == "__main__":
    unittest.main()
