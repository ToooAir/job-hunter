"""Regression tests for the GTJ / Levels.fyi cache freshness check.

Both scrapers store `fetched_at` as a NAIVE UTC ISO string but compared it
against an AWARE `datetime.now(timezone.utc)`. The resulting TypeError was
swallowed by `except Exception: return False`, so the cache never hit and
every salary estimate re-scraped everything (~18 s of a 22 s run).

Run:  python -m unittest tests.test_salary_cache -v
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import gtj_salary_scraper, levels_scraper  # noqa: E402


def _naive_utc(days_ago: float) -> str:
    """Timestamp in the exact format both scrapers write (naive, no offset)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class IsFreshTest(unittest.TestCase):
    MODULES = (
        (gtj_salary_scraper, gtj_salary_scraper.CACHE_TTL_DAYS),
        (levels_scraper, levels_scraper.CACHE_TTL_DAYS),
    )

    def test_naive_recent_timestamp_is_fresh(self):
        # The regression: naive timestamps must not blow up the aware
        # subtraction, otherwise the cache silently never hits.
        for mod, _ttl in self.MODULES:
            with self.subTest(module=mod.__name__):
                entry = {"fetched_at": _naive_utc(days_ago=0.5)}
                self.assertTrue(mod._is_fresh(entry))

    def test_naive_expired_timestamp_is_stale(self):
        for mod, ttl in self.MODULES:
            with self.subTest(module=mod.__name__):
                entry = {"fetched_at": _naive_utc(days_ago=ttl + 1)}
                self.assertFalse(mod._is_fresh(entry))

    def test_aware_timestamp_still_works(self):
        for mod, _ttl in self.MODULES:
            with self.subTest(module=mod.__name__):
                entry = {"fetched_at": datetime.now(timezone.utc).isoformat()}
                self.assertTrue(mod._is_fresh(entry))

    def test_garbage_and_missing_are_stale(self):
        for mod, _ttl in self.MODULES:
            with self.subTest(module=mod.__name__):
                self.assertFalse(mod._is_fresh({"fetched_at": "not-a-date"}))
                self.assertFalse(mod._is_fresh({}))


if __name__ == "__main__":
    unittest.main()
