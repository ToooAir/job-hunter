"""Tests for the Bundesagentur circuit breaker in phase1_ingestor.

Background (measured 2026-08-17): api.arbeitsagentur.de answers every request
with an HTML maintenance page under HTTP 200. Across the full log history that
is 13,083 calls, 0 successes, and 0 rows in the corpus — so the daily run was
spending 5m32s walking the whole keyword × location matrix to rediscover an
outage the third request already proved.

Run:  python -m unittest discover tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import phase1_ingestor as ing  # noqa: E402

MAINTENANCE_HTML = (
    b"<!DOCTYPE html>\n<html><head><title>Wartungsarbeiten</title></head>"
    b"<body><p>Aufgrund von Wartungsarbeiten steht Ihnen die Webseite "
    b"aktuell nicht zur Verfuegung.</p></body></html>"
)


class _Resp:
    """Minimal requests.Response stand-in."""

    def __init__(self, content=b"{}", status=200, json_data=None):
        self.content = content
        self.status_code = status
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json


class MaintenancePageTest(unittest.TestCase):
    def test_maintenance_page_is_named_in_the_log(self):
        """A 200 + HTML slips past raise_for_status and dies in .json() with an
        opaque 'Expecting value: line 1 column 1'. Say what actually happened."""
        with mock.patch.object(ing.requests, "get", return_value=_Resp(MAINTENANCE_HTML)):
            with self.assertLogs("phase1_ingestor", level="WARNING") as caught:
                out = ing._ba_safe_get(f"{ing.BA_API}/jobs", {"was": "x"})
        self.assertIsNone(out)
        log = "\n".join(caught.output)
        self.assertIn("maintenance page", log)
        self.assertNotIn("Expecting value", log)

    def test_no_politeness_sleep_when_the_call_did_not_work(self):
        """1.5s between *working* calls is courtesy; 1.5s between failures is
        just 5 minutes a day of nothing."""
        with mock.patch.object(ing.requests, "get", return_value=_Resp(MAINTENANCE_HTML)):
            with mock.patch.object(ing.time, "sleep") as slept:
                ing._ba_safe_get(f"{ing.BA_API}/jobs", {"was": "x"})
        slept.assert_not_called()


class CircuitBreakerTest(unittest.TestCase):
    def _run(self, response_factory, keywords, locations):
        calls = {"n": 0}

        def _get(*args, **kwargs):
            calls["n"] += 1
            return response_factory(calls["n"])

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"), \
             mock.patch.object(ing, "upsert_job", return_value=True):
            added, skipped = ing.scrape_bundesagentur(
                conn=None,
                keywords=keywords,
                locations=locations,
                radius_km=50,
                include_remote=False,
                size=100,
            )
        return calls["n"], added, skipped

    def test_gives_up_after_three_consecutive_failures(self):
        keywords = [f"kw{i}" for i in range(22)]
        locations = ["Hamburg", "Berlin", "Munich"]  # 66 queries if it ran the matrix

        calls, added, _ = self._run(lambda n: _Resp(MAINTENANCE_HTML), keywords, locations)

        self.assertEqual(calls, ing.BA_MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(added, 0)

    def test_a_single_transient_failure_does_not_abandon_the_run(self):
        """The breaker must trip on an outage, not on one flaky request."""
        good = {"stellenangebote": []}

        def factory(n):
            return _Resp(MAINTENANCE_HTML) if n == 2 else _Resp(b"{}", json_data=good)

        keywords = ["kw0", "kw1", "kw2", "kw3"]
        calls, _, _ = self._run(factory, keywords, ["Hamburg"])

        self.assertEqual(calls, 4)  # all four queries attempted

    def test_a_working_endpoint_runs_the_whole_matrix(self):
        good = {"stellenangebote": []}
        keywords = ["kw0", "kw1", "kw2"]
        locations = ["Hamburg", "Berlin"]

        calls, _, _ = self._run(lambda n: _Resp(b"{}", json_data=good), keywords, locations)

        self.assertEqual(calls, 6)


if __name__ == "__main__":
    unittest.main()
