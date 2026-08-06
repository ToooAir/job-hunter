"""Tests for utils/ats_harvest.py (direct-ATS seed harvest).

Run:  python -m unittest tests.test_ats_harvest -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.ats_harvest import (  # noqa: E402
    extract_ats_slug,
    harvest_ats_seeds,
    merged_companies,
)


class ExtractSlugTest(unittest.TestCase):
    def test_greenhouse_variants(self):
        self.assertEqual(extract_ats_slug(
            "greenhouse", "https://job-boards.greenhouse.io/cresta/jobs/4668107008"), "cresta")
        self.assertEqual(extract_ats_slug(
            "greenhouse", "https://boards.greenhouse.io/contentful/jobs/1"), "contentful")
        self.assertEqual(extract_ats_slug(
            "greenhouse", "https://job-boards.eu.greenhouse.io/charles/jobs/487510"), "charles")
        # the embed-form apply_url carries the tenant in ?for=
        self.assertEqual(extract_ats_slug(
            "greenhouse", "https://boards.greenhouse.io/embed/job_board/js?for=fundedclub"),
            "fundedclub")

    def test_ashby_lever_workable_smartrecruiters(self):
        self.assertEqual(extract_ats_slug(
            "ashby", "https://jobs.ashbyhq.com/enpal/208c7c58-uuid"), "enpal")
        # ashby tenants can carry a domain suffix
        self.assertEqual(extract_ats_slug(
            "ashby", "https://jobs.ashbyhq.com/taxfix.com/d22dda5c-uuid"), "taxfix.com")
        self.assertEqual(extract_ats_slug(
            "lever", "https://jobs.lever.co/canarytechnologies/bb2b3ada"), "canarytechnologies")
        self.assertEqual(extract_ats_slug(
            "workable", "https://apply.workable.com/fioneer/j/35849DA598/"), "fioneer")

    def test_personio_subdomain(self):
        self.assertEqual(extract_ats_slug(
            "personio", "https://peter-park.jobs.personio.de/job/2655559?apply"), "peter-park")
        self.assertEqual(extract_ats_slug(
            "personio", "https://matrix42.jobs.personio.com/job/2659375"), "matrix42")

    def test_falls_back_to_url_when_apply_url_missing(self):
        self.assertEqual(extract_ats_slug(
            "ashby", None, "https://jobs.ashbyhq.com/taktile/80550a85"), "taktile")

    def test_rejects_non_tenant_and_mismatched_host(self):
        # ATS routing segments and the ATS's own name are not tenants
        self.assertIsNone(extract_ats_slug("ashby", "https://jobs.ashbyhq.com/ashby/x"))
        self.assertIsNone(extract_ats_slug("greenhouse", "https://boards.greenhouse.io/embed/"))
        # apply_url on a different host than the declared ats → no slug
        self.assertIsNone(extract_ats_slug("greenhouse", "https://jobs.ashbyhq.com/foo/x"))
        self.assertIsNone(extract_ats_slug("lever", ""))
        self.assertIsNone(extract_ats_slug("lever", None, None))
        # recruitee junk that leaked into apply_url must not parse as lever/etc
        self.assertIsNone(extract_ats_slug(
            "workable", "https://careers-analytics.recruitee.com&quot;,&quot;app"))


class HarvestSeedsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE jobs (id TEXT, ats TEXT, location TEXT, apply_url TEXT, url TEXT)")
        rows = [
            # (id, ats, location, apply_url, url)
            ("1", "ashby", "Berlin, Germany", "https://jobs.ashbyhq.com/taktile/a", None),
            ("2", "ashby", "Remote", "https://jobs.ashbyhq.com/taktile/b", None),   # dup slug
            ("3", "greenhouse", "Munich", "https://job-boards.greenhouse.io/cresta/jobs/1", None),
            ("4", "lever", "Hamburg", "https://jobs.lever.co/netlight/x", None),
            ("5", "personio", "Köln", "https://envelio.jobs.personio.de/job/1", None),
            # pure non-DE tenant — the geo gate must drop it
            ("6", "greenhouse", "San Francisco, United States", "https://boards.greenhouse.io/scaleai/jobs/1", None),
            # a job with no resolvable slug is ignored
            ("7", "ashby", "Berlin", None, "https://www.welcometothejungle.com/x"),
        ]
        self.conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", rows)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_harvest_groups_and_dedups(self):
        seeds = harvest_ats_seeds(self.conn)
        self.assertEqual(seeds.get("ashby"), ["taktile"])   # deduped across 2 rows
        self.assertEqual(seeds.get("lever"), ["netlight"])
        self.assertEqual(seeds.get("personio"), ["envelio"])

    def test_geo_gate_drops_pure_non_de_tenant(self):
        seeds = harvest_ats_seeds(self.conn, geo_gate=True)
        self.assertIn("cresta", seeds.get("greenhouse", []))
        self.assertNotIn("scaleai", seeds.get("greenhouse", []))

    def test_geo_gate_off_keeps_all(self):
        seeds = harvest_ats_seeds(self.conn, geo_gate=False)
        self.assertIn("scaleai", seeds.get("greenhouse", []))


class MergedCompaniesTest(unittest.TestCase):
    def test_appends_new_and_dedups_case_insensitively(self):
        seeds = {"ashby": ["Taktile", "enpal", "newco"]}
        # config already has enpal (different case) — must not duplicate
        merged = merged_companies(["enpal", "n8n"], "ashby", seeds)
        self.assertEqual(merged, ["enpal", "n8n", "Taktile", "newco"])

    def test_empty_config_and_missing_ats(self):
        self.assertEqual(merged_companies(None, "lever", {"lever": ["mistral"]}), ["mistral"])
        self.assertEqual(merged_companies(["a"], "greenhouse", {}), ["a"])


if __name__ == "__main__":
    unittest.main()
