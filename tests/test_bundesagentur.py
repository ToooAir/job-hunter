"""Tests for the Bundesagentur für Arbeit scraper in phase1_ingestor.

History worth keeping: the source produced 13,083 failed calls and zero rows
between 2026-04 and 2026-08 because api.arbeitsagentur.de/jobsuche/v2 was
decommissioned and answers everything with an HTML maintenance page under HTTP
200 — raise_for_status passes, .json() dies with "Expecting value: line 1
column 1", and the daily run spent 5m32s rediscovering it.

The live endpoint is the one the public search SPA calls (read off
/jobsuche/config/config.js): pc/v6 for search, pc/v4/jobdetails keyed on the
base64 of the Referenznummer. The schema changed wholesale, so these tests pin
the field mapping — a silent rename is how this source died unnoticed once.

Run:  python -m unittest discover tests -v
"""

import base64
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

REFNR = "13644-299674-S"


def _listing(refnr=REFNR, firma="zollsoft GmbH", extern=None):
    return {
        "referenznummer": refnr,
        "firma": firma,
        "stellenangebotsTitel": "DevOps Engineer (m/w/d)",
        "stellenlokationen": [
            {"adresse": {"plz": "22085", "ort": "Hamburg", "land": "DEUTSCHLAND"}}
        ],
        **({"externeURL": extern} if extern else {}),
    }


def _search_page(entries, total=None):
    return {
        "ergebnisliste": entries,
        "maxErgebnisse": total if total is not None else len(entries),
        "page": 1,
        "size": 100,
    }


def _detail(desc="Wir suchen eine Entwicklerin. " * 12, allianz=None):
    return {
        "stellenangebotsTitel": "DevOps Engineer (m/w/d)",
        "stellenangebotsBeschreibung": desc,
        "referenznummer": REFNR,
        **({"allianzpartnerUrl": allianz} if allianz else {}),
    }


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


class _FakeConn:
    """Stands in for the sqlite connection: records upserts, answers _url_in_db."""

    def __init__(self, known_urls=()):
        self.known = set(known_urls)
        self.rows = []

    def execute(self, sql, params=()):
        conn = self

        class _Cur:
            def fetchone(self):
                # _url_in_db asks "SELECT 1 FROM jobs WHERE id = ? OR url = ?"
                return (1,) if params and params[-1] in conn.known else None

        return _Cur()


class EndpointTest(unittest.TestCase):
    def test_targets_the_live_host_not_the_decommissioned_one(self):
        self.assertIn("rest.arbeitsagentur.de", ing.BA_SEARCH)
        self.assertIn("/pc/v6/jobs", ing.BA_SEARCH)
        self.assertNotIn("api.arbeitsagentur.de", ing.BA_SEARCH)

    def test_uses_the_only_api_key_that_returns_200(self):
        """'jobboerse' and no key both 403 on this host; only this value works."""
        self.assertEqual(ing.BA_HEADERS["X-API-Key"], "jobboerse-jobsuche")

    def test_detail_is_keyed_on_the_base64_of_the_reference_number(self):
        """The raw Referenznummer 404s — only the base64 form resolves."""
        seen = {}

        def _get(url, **kwargs):
            seen["url"] = url
            return _Resp(b"{}", json_data=_detail())

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"):
            ing._ba_detail(REFNR)

        encoded = base64.b64encode(REFNR.encode()).decode()
        self.assertTrue(seen["url"].endswith(encoded))
        self.assertNotIn(REFNR, seen["url"])


class MaintenancePageTest(unittest.TestCase):
    def test_maintenance_page_is_named_in_the_log(self):
        with mock.patch.object(ing.requests, "get", return_value=_Resp(MAINTENANCE_HTML)):
            with self.assertLogs("phase1_ingestor", level="WARNING") as caught:
                out = ing._ba_safe_get(ing.BA_SEARCH, {"was": "x"})
        self.assertIsNone(out)
        log = "\n".join(caught.output)
        self.assertIn("maintenance page", log)
        self.assertNotIn("Expecting value", log)

    def test_no_politeness_sleep_when_the_call_did_not_work(self):
        with mock.patch.object(ing.requests, "get", return_value=_Resp(MAINTENANCE_HTML)):
            with mock.patch.object(ing.time, "sleep") as slept:
                ing._ba_safe_get(ing.BA_SEARCH, {"was": "x"})
        slept.assert_not_called()


class SchemaMappingTest(unittest.TestCase):
    """v6 renamed every field this scraper reads. Pin the mapping."""

    def _scrape(self, listing, detail, known_urls=()):
        captured = []

        def _get(url, **kwargs):
            if "/jobdetails/" in url:
                return _Resp(b"{}", json_data=detail)
            return _Resp(b"{}", json_data=_search_page([listing]))

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"), \
             mock.patch.object(ing, "upsert_job",
                               side_effect=lambda c, rec: captured.append(rec) or True):
            ing.scrape_bundesagentur(
                conn=_FakeConn(known_urls), keywords=["AI Engineer"],
                locations=["Hamburg"], radius_km=50, include_remote=False, size=10,
            )
        return captured

    def test_list_and_detail_fields_land_in_the_record(self):
        rec = self._scrape(_listing(), _detail(desc="Wir bauen Pipelines. " * 20))[0]

        self.assertEqual(rec["company"], "zollsoft GmbH")        # firma
        self.assertEqual(rec["title"], "DevOps Engineer (m/w/d)")  # stellenangebotsTitel
        self.assertEqual(rec["location"], "Hamburg, DEUTSCHLAND")  # stellenlokationen
        self.assertIn(REFNR, rec["url"])                           # referenznummer
        self.assertIn("Pipelines", rec["raw_jd_text"])             # ...Beschreibung
        self.assertEqual(rec["source"], "bundesagentur")

    def test_v2_field_names_would_now_yield_nothing(self):
        """Guard against a silent revert to the old schema: a v2-shaped payload
        must not quietly produce a record with an empty company and title."""
        v2_listing = {"refnr": REFNR, "arbeitgeber": "zollsoft GmbH",
                      "titel": "DevOps Engineer", "arbeitsort": {"ort": "Hamburg"}}
        captured = []

        def _get(url, **kwargs):
            return _Resp(b"{}", json_data={"stellenangebote": [v2_listing]})

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"), \
             mock.patch.object(ing, "upsert_job",
                               side_effect=lambda c, rec: captured.append(rec) or True):
            ing.scrape_bundesagentur(
                conn=_FakeConn(), keywords=["AI Engineer"], locations=["Hamburg"],
                radius_km=50, include_remote=False, size=10,
            )
        self.assertEqual(captured, [])  # no 'ergebnisliste' → nothing ingested


class ApplyChannelTest(unittest.TestCase):
    """~80% of BA listings hide employer contact behind a BA login. Those are
    ingested anyway — a JD naming a company not yet in the corpus is worth
    chasing by hand — with apply_url == url marking the walled ones."""

    def _rec(self, listing, detail):
        captured = []

        def _get(url, **kwargs):
            if "/jobdetails/" in url:
                return _Resp(b"{}", json_data=detail)
            return _Resp(b"{}", json_data=_search_page([listing]))

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"), \
             mock.patch.object(ing, "upsert_job",
                               side_effect=lambda c, rec: captured.append(rec) or True):
            ing.scrape_bundesagentur(
                conn=_FakeConn(), keywords=["AI Engineer"], locations=["Hamburg"],
                radius_km=50, include_remote=False, size=10,
            )
        return captured[0]

    def test_external_url_becomes_the_apply_url(self):
        rec = self._rec(_listing(extern="https://zollsoft.de/jobs/devops"), _detail())
        self.assertEqual(rec["apply_url"], "https://zollsoft.de/jobs/devops")
        self.assertNotEqual(rec["apply_url"], rec["url"])

    def test_allianzpartner_url_is_the_second_choice(self):
        rec = self._rec(_listing(), _detail(allianz="https://get-in-it.de/p123"))
        self.assertEqual(rec["apply_url"], "https://get-in-it.de/p123")

    def test_walled_listing_is_kept_and_points_at_the_public_ba_page(self):
        rec = self._rec(_listing(), _detail())
        self.assertEqual(rec["apply_url"], rec["url"])
        self.assertIn("arbeitsagentur.de/jobsuche/jobdetail/", rec["apply_url"])

    def test_a_bare_host_is_not_an_apply_channel(self):
        """Measured over 2,155 rows: 33% of externeURL values are a scheme-less
        bare host. It is the employer's homepage, it does not even open as a
        link from the draft card, and it drove nav-error 11/11 in Stage 1."""
        for junk in ("www.zalando.de", "www.plusyou.de", "www.aero-hp.com"):
            rec = self._rec(_listing(extern=junk), _detail())
            self.assertEqual(rec["apply_url"], rec["url"], junk)

    def test_a_self_reference_to_ba_is_not_an_apply_channel(self):
        """34% of them say http://www.arbeitsagentur.de — "apply through us".
        Storing that sends the human to a search homepage, not the posting."""
        rec = self._rec(_listing(extern="http://www.arbeitsagentur.de"), _detail())
        self.assertEqual(rec["apply_url"], rec["url"])
        self.assertIn("/jobsuche/jobdetail/", rec["apply_url"])

    def test_a_partner_board_deep_link_survives_the_gate(self):
        # the 32% that are real: don't let the gate eat them
        rec = self._rec(_listing(), _detail(allianz="https://www.get-in-it.de/jobsuche/stelle/9"))
        self.assertEqual(rec["apply_url"], "https://www.get-in-it.de/jobsuche/stelle/9")

    def test_the_gate_is_idempotent_on_an_already_repaired_row(self):
        # the backfill re-derives from the stored value; a BA page must stay put
        job_url = "https://www.arbeitsagentur.de/jobsuche/jobdetail/13644-299674-S"
        self.assertEqual(ing._ba_apply_url(job_url, job_url), job_url)
        self.assertEqual(ing._ba_apply_url("https://x.de/jobs/1", "u"), "https://x.de/jobs/1")

    def test_the_walled_marker_does_not_touch_the_user_owned_notes_field(self):
        """`notes` holds interview impressions and abandon reasons the user
        writes and saves; an ingest-time marker there would be clobbered."""
        rec = self._rec(_listing(), _detail())
        self.assertFalse(rec.get("notes"))


class DetailCallCostTest(unittest.TestCase):
    """One detail call per posting is the expensive half of this source, and
    only ~13% of what search returns was published in the last week."""

    def test_known_url_costs_no_detail_call(self):
        detail_calls = []

        def _get(url, **kwargs):
            if "/jobdetails/" in url:
                detail_calls.append(url)
                return _Resp(b"{}", json_data=_detail())
            return _Resp(b"{}", json_data=_search_page([_listing()]))

        job_url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{REFNR}"
        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"), \
             mock.patch.object(ing, "upsert_job") as up:
            _, skipped = ing.scrape_bundesagentur(
                conn=_FakeConn(known_urls=[job_url]), keywords=["AI Engineer"],
                locations=["Hamburg"], radius_km=50, include_remote=False, size=10,
            )

        self.assertEqual(detail_calls, [])
        up.assert_not_called()
        self.assertEqual(skipped, 1)

    def test_unknown_url_does_fetch_the_detail(self):
        detail_calls = []

        def _get(url, **kwargs):
            if "/jobdetails/" in url:
                detail_calls.append(url)
                return _Resp(b"{}", json_data=_detail())
            return _Resp(b"{}", json_data=_search_page([_listing()]))

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"), \
             mock.patch.object(ing, "upsert_job", return_value=True):
            ing.scrape_bundesagentur(
                conn=_FakeConn(), keywords=["AI Engineer"], locations=["Hamburg"],
                radius_km=50, include_remote=False, size=10,
            )
        self.assertEqual(len(detail_calls), 1)


class PagingTest(unittest.TestCase):
    def test_search_asks_for_page_one_not_zero(self):
        """v6 pages are 1-based; page=0 returns nothing at all."""
        params_seen = {}

        def _get(url, **kwargs):
            if "/jobdetails/" not in url:
                params_seen.update(kwargs.get("params") or {})
            return _Resp(b"{}", json_data=_search_page([]))

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"):
            ing.scrape_bundesagentur(
                conn=_FakeConn(), keywords=["AI Engineer"], locations=["Hamburg"],
                radius_km=50, include_remote=False, size=10,
            )
        self.assertEqual(params_seen.get("page"), 1)


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
                conn=_FakeConn(), keywords=keywords, locations=locations,
                radius_km=50, include_remote=False, size=100,
            )
        return calls["n"], added, skipped

    def test_gives_up_after_three_consecutive_failures(self):
        keywords = [f"kw{i}" for i in range(22)]
        locations = ["Hamburg", "Berlin", "Munich"]  # 66 queries if it ran the matrix

        calls, added, _ = self._run(lambda n: _Resp(MAINTENANCE_HTML), keywords, locations)

        self.assertEqual(calls, ing.BA_MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(added, 0)

    def test_a_single_transient_failure_does_not_abandon_the_run(self):
        def factory(n):
            if n == 2:
                return _Resp(MAINTENANCE_HTML)
            return _Resp(b"{}", json_data=_search_page([]))

        calls, _, _ = self._run(factory, ["kw0", "kw1", "kw2", "kw3"], ["Hamburg"])
        self.assertEqual(calls, 4)  # all four queries attempted

    def test_a_working_endpoint_runs_the_whole_matrix(self):
        calls, _, _ = self._run(
            lambda n: _Resp(b"{}", json_data=_search_page([])),
            ["kw0", "kw1", "kw2"], ["Hamburg", "Berlin"],
        )
        self.assertEqual(calls, 6)


if __name__ == "__main__":
    unittest.main()
