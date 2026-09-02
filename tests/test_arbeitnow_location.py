"""Tests for phase1_ingestor._arbeitnow_location_ok (nationwide recall).

phase1_ingestor imports requests/yaml at module level — run inside the container:
    docker exec job-hunter-pipeline-1 python3 -m unittest tests.test_arbeitnow_location -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase1_ingestor import _arbeitnow_location_ok  # noqa: E402

LEGACY = [s.lower() for s in ["Hamburg", "Berlin", "Munich", "Remote", "Germany"]]


class TestArbeitnowLocationOk(unittest.TestCase):
    def ok(self, loc, remote=False, remote_filter=True):
        return _arbeitnow_location_ok(loc, remote, LEGACY, remote_filter)

    def test_legacy_substrings_still_pass(self):
        self.assertTrue(self.ok("Hamburg"))
        self.assertTrue(self.ok("Kiel, Germany"))
        self.assertTrue(self.ok("Remote"))

    def test_small_german_cities_now_pass(self):
        # the 2026-09-02 gap: non-remote postings outside the 19-city list
        self.assertTrue(self.ok("Aachen"))
        self.assertTrue(self.ok("Ulm"))
        self.assertTrue(self.ok("Münster, Nordrhein-Westfalen"))
        self.assertTrue(self.ok("74076 Heilbronn"))
        self.assertTrue(self.ok("Regensburg, Bavaria"))

    def test_foreign_cities_still_dropped(self):
        self.assertFalse(self.ok("Madrid"))
        self.assertFalse(self.ok("Vienna, Austria"))
        self.assertFalse(self.ok("Zurich"))
        self.assertFalse(self.ok("Halle, Belgium"))

    def test_remote_flag_passes_any_location(self):
        self.assertTrue(self.ok("Lisbon", remote=True))
        self.assertFalse(self.ok("Lisbon", remote=True, remote_filter=False))

    def test_unknown_bare_town_is_dropped(self):
        # not in any geography list and not remote — same as before
        self.assertFalse(self.ok("Springfield"))
        self.assertFalse(self.ok(""))


if __name__ == "__main__":
    unittest.main()
