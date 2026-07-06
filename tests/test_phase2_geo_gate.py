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


if __name__ == "__main__":
    unittest.main()
