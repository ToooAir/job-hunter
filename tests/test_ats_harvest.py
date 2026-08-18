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
    greenhouse_job_id,
    merged_companies,
    normalize_greenhouse_apply_url,
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


class NormalizeGreenhouseApplyUrlTest(unittest.TestCase):
    LOADER = "https://boards.greenhouse.io/embed/job_board/js?for=fundedclub"

    def test_posting_id_from_the_listing_url_becomes_the_form(self):
        self.assertEqual(
            normalize_greenhouse_apply_url(
                self.LOADER, "https://funded.club/jobs.html?gh_jid=7665743003"),
            "https://job-boards.greenhouse.io/embed/job_app"
            "?for=fundedclub&token=7665743003")

    def test_posting_id_on_the_loader_itself_is_used(self):
        self.assertEqual(
            normalize_greenhouse_apply_url(
                "https://boards.greenhouse.io/embed/job_board/js"
                "?for=moonfare&gh_jid=7773348003"),
            "https://job-boards.greenhouse.io/embed/job_app"
            "?for=moonfare&token=7773348003")

    def test_without_a_posting_id_falls_back_to_the_board(self):
        # still an HTML page, unlike the loader script
        self.assertEqual(
            normalize_greenhouse_apply_url(self.LOADER, "https://funded.club/jobs.html"),
            "https://job-boards.greenhouse.io/embed/job_board?for=fundedclub")
        self.assertEqual(
            normalize_greenhouse_apply_url(self.LOADER, None),
            "https://job-boards.greenhouse.io/embed/job_board?for=fundedclub")

    def test_non_numeric_gh_jid_is_ignored(self):
        self.assertEqual(
            normalize_greenhouse_apply_url(self.LOADER, "https://x.de/j?gh_jid=abc"),
            "https://job-boards.greenhouse.io/embed/job_board?for=fundedclub")

    def test_everything_else_passes_through_untouched(self):
        for url in (
            "https://job-boards.greenhouse.io/cresta/jobs/4668107008",
            "https://boards.greenhouse.io/embed/job_board?for=fundedclub",
            "https://jobs.lever.co/example/45b0fae3/apply",
            "",
            None,
        ):
            self.assertEqual(normalize_greenhouse_apply_url(url), url)

    def test_loader_without_a_usable_slug_is_left_alone(self):
        # nothing to build from → hand it back so the asset guard rejects it
        for url in ("https://boards.greenhouse.io/embed/job_board/js",
                    "https://boards.greenhouse.io/embed/job_board/js?for="):
            self.assertEqual(normalize_greenhouse_apply_url(url), url)


class GreenhouseJobIdTest(unittest.TestCase):
    def test_gh_jid_query_param(self):
        self.assertEqual(
            greenhouse_job_id("https://funded.club/jobs.html?gh_jid=7665743003"),
            "7665743003")

    def test_greenhouse_hosted_path(self):
        self.assertEqual(
            greenhouse_job_id("https://job-boards.greenhouse.io/cresta/jobs/4668107008"),
            "4668107008")
        self.assertEqual(
            greenhouse_job_id("https://job-boards.eu.greenhouse.io/charles/jobs/487510"),
            "487510")

    def test_token_param_only_counts_on_greenhouse(self):
        # the embed form this module builds spells the id as ?token=
        self.assertEqual(greenhouse_job_id(
            "https://job-boards.greenhouse.io/embed/job_app?for=moonfare&token=7773348003"),
            "7773348003")
        # …elsewhere ?token= is somebody else's auth token, not a posting id
        self.assertIsNone(greenhouse_job_id("https://acme.de/apply?token=12345"))

    def test_first_hit_wins_and_none_when_absent(self):
        self.assertEqual(
            greenhouse_job_id(None, "https://x.de/j?gh_jid=42"), "42")
        self.assertIsNone(greenhouse_job_id("https://x.de/careers/engineer", None))
        # a /jobs/<slug> path on a non-greenhouse host is not a posting id
        self.assertIsNone(greenhouse_job_id("https://x.de/jobs/engineer"))
        self.assertIsNone(greenhouse_job_id())


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
