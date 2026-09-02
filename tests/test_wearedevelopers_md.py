"""Tests for the 2026-09-02 wearedevelopers scraper (Markdown endpoints).

phase1_ingestor imports requests/yaml at module level — run inside the container:
    docker exec job-hunter-pipeline-1 python3 -m unittest tests.test_wearedevelopers_md -v
"""

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import phase1_ingestor as ing  # noqa: E402

LISTING_MD = """> Markdown version of [/jobs?country=DE&q=Python](https://www.wearedevelopers.com/jobs?country=DE&q=Python).

---

# Developer Jobs

Region: Germany

200 jobs found matching "Python"

## Python Developer

- **Company:** MY GAMES
- **Location:** Germany (Remote available)
- **Contract:** Permanent contract
- **Published:** July 9, 2026
- [View job](https://www.wearedevelopers.com/jobs/ext/1210076-python-developer)
- [Apply](https://de.indeed.com/viewjob?jk=2203962aace02a42)

## Software Development Engineer - PYTHON PACKAGING

- **Company:** AMD
- **Location:** München, Germany
- **Experience:** Expert
- **Contract:** Permanent contract
- **Published:** August 23, 2026
- [View job](https://www.wearedevelopers.com/jobs/ext/2194785-software-development-engineer-python-packaging)
- [Apply](https://www.jobfinder.de/job/senior-sde/)

## Related reading

- [Finding Jobs in Germany](https://www.wearedevelopers.com/magazine/375-finding-jobs-in-germany)
"""

DETAIL_MD = """> Markdown version of [/jobs/ext/1164541-python-developer-in-germany](https://www.wearedevelopers.com/jobs/ext/1164541-python-developer-in-germany).

---

# Python Developer in Germany

- **Company:** Andersen Software
- **Location:** Hamburg, Germany (Remote available)
- **Contract:** Permanent contract
- **Skills:** Python (Programming Language), PostgreSQL, Fastapi
- **Published:** July 3, 2026
- **Apply:** https://de.indeed.com/viewjob?jk=af60fd58a3e381e6

## About the Role

* Strong Python backend development (Python 3.11+) for 5+ years.
 * Level of German - from Upper-Intermediate and above.

## Description

Andersen is hiring a Python Developer in Germany for a project improving system
monitoring and data-driven process optimization.

## Related articles

- [Developer Salary in Germany [2023]](https://www.wearedevelopers.com/magazine/195-salary)
- [Finding Jobs in Germany](https://www.wearedevelopers.com/magazine/375-finding-jobs-in-germany)
"""


class ParseListingTest(unittest.TestCase):
    def test_every_job_section_is_parsed_and_header_sections_are_not(self):
        jobs = ing._wad_parse_listing(LISTING_MD)
        self.assertEqual([j["title"] for j in jobs],
                         ["Python Developer", "Software Development Engineer - PYTHON PACKAGING"])
        j = jobs[0]
        self.assertEqual(j["company"], "MY GAMES")
        self.assertEqual(j["location"], "Germany (Remote available)")
        self.assertEqual(j["url"], "https://www.wearedevelopers.com/jobs/ext/1210076-python-developer")
        self.assertEqual(j["apply_url"], "https://de.indeed.com/viewjob?jk=2203962aace02a42")
        self.assertEqual(j["published"], "July 9, 2026")

    def test_empty_page_yields_nothing(self):
        self.assertEqual(ing._wad_parse_listing("# Developer Jobs\n\n0 jobs found"), [])


class ParseDetailTest(unittest.TestCase):
    def test_jd_is_the_prose_sections_only(self):
        jd, apply_url, loc = ing._wad_parse_detail(DETAIL_MD)
        self.assertIn("About the Role", jd)
        self.assertIn("Strong Python backend development", jd)
        self.assertIn("Andersen is hiring", jd)
        self.assertNotIn("Related articles", jd)
        self.assertNotIn("magazine/375", jd)
        self.assertNotIn("**Company:**", jd)       # header fields are not JD prose
        self.assertEqual(apply_url, "https://de.indeed.com/viewjob?jk=af60fd58a3e381e6")
        self.assertEqual(loc, "Hamburg, Germany (Remote available)")


class _Resp:
    def __init__(self, text, ctype="text/plain; charset=utf-8", status=200):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class PublishedDateTest(unittest.TestCase):
    def test_parses_the_listing_shape(self):
        self.assertEqual(ing._wad_published_date("July 9, 2026"), dt.date(2026, 7, 9))
        self.assertIsNone(ing._wad_published_date("2026-07-09"))
        self.assertIsNone(ing._wad_published_date(""))
        self.assertIsNone(ing._wad_published_date(None))


class ScrapeTest(unittest.TestCase):
    TODAY = dt.date(2026, 8, 15)   # July 9 listing is 37 days old → still within the 45-day TTL

    def _run(self, known_urls=(), max_pages=3, today=None, ledger=()):
        captured, calls, marked = [], [], []

        def _get(url, **kwargs):
            calls.append((url, dict(kwargs.get("params") or {})))
            if url.endswith(".md") and "/jobs/ext/" in url:
                return _Resp(DETAIL_MD)
            if url.endswith("/jobs.md"):
                page = kwargs["params"]["page"]
                return _Resp(LISTING_MD if page == 1 else "# Developer Jobs\n\n0 jobs found")
            return _Resp("", status=404)

        with mock.patch.object(ing.requests, "get", side_effect=_get), \
             mock.patch.object(ing.time, "sleep"), \
             mock.patch.object(ing, "_wad_today", return_value=today or self.TODAY), \
             mock.patch.object(ing, "load_seen_not_stored", return_value=set(ledger)), \
             mock.patch.object(ing, "mark_seen_not_stored",
                               side_effect=lambda c, u, s: marked.append(u)), \
             mock.patch.object(ing, "_url_in_db", side_effect=lambda c, u: u in known_urls), \
             mock.patch.object(ing, "upsert_job",
                               side_effect=lambda c, rec: captured.append(rec) or
                               rec["url"] not in self.dup_urls):
            added, skipped = ing.scrape_wearedevelopers(conn=None, keywords=["Python"], max_pages=max_pages)
        self.marked = marked
        return added, skipped, captured, calls

    dup_urls: tuple = ()

    def test_listing_markdown_is_requested_for_germany(self):
        _, _, _, calls = self._run()
        url, params = calls[0]
        self.assertEqual(url, "https://www.wearedevelopers.com/jobs.md")
        self.assertEqual(params, {"country": "DE", "q": "Python", "page": 1})

    def test_records_carry_jd_from_the_detail_page_and_the_external_apply_url(self):
        added, _, captured, _ = self._run()
        self.assertEqual(added, 2)
        rec = captured[0]
        self.assertEqual(rec["source"], "wearedevelopers")
        self.assertEqual(rec["url"], "https://www.wearedevelopers.com/jobs/ext/1210076-python-developer")
        self.assertIn("Strong Python backend development", rec["raw_jd_text"])
        self.assertEqual(rec["apply_url"], "https://de.indeed.com/viewjob?jk=af60fd58a3e381e6")
        self.assertEqual(rec["location"], "Hamburg, Germany (Remote available)")

    def test_known_urls_skip_the_detail_fetch(self):
        known = ("https://www.wearedevelopers.com/jobs/ext/1210076-python-developer",)
        added, skipped, _, calls = self._run(known_urls=known)
        self.assertEqual(added, 1)
        detail_urls = [u for u, _ in calls if "/jobs/ext/" in u]
        self.assertEqual(len(detail_urls), 1)
        self.assertNotIn(known[0] + ".md", detail_urls)

    def test_expires_at_is_aged_from_the_published_date(self):
        _, _, captured, _ = self._run()
        # "July 9, 2026" + 45 days = 2026-08-23
        self.assertEqual(captured[0]["expires_at"], "2026-08-23T00:00:00")

    def test_listings_older_than_the_ttl_are_not_ingested(self):
        # on 2026-09-03 the July 9 listing is 56 days old → skipped before any
        # detail fetch; the August 23 one is 11 days old → ingested
        added, _, captured, calls = self._run(today=dt.date(2026, 9, 3))
        self.assertEqual(added, 1)
        self.assertEqual(captured[0]["url"],
                         "https://www.wearedevelopers.com/jobs/ext/2194785-software-development-engineer-python-packaging")
        self.assertNotIn("https://www.wearedevelopers.com/jobs/ext/1210076-python-developer.md",
                         [u for u, _ in calls])

    def test_jd_hash_duplicates_enter_the_seen_ledger_and_skip_next_time(self):
        self.dup_urls = ("https://www.wearedevelopers.com/jobs/ext/1210076-python-developer",)
        try:
            added, skipped, _, _ = self._run()
            self.assertEqual((added, skipped), (1, 1))
            self.assertEqual(self.marked, list(self.dup_urls))
            # next run: the ledger short-circuits before the detail fetch
            _, _, _, calls = self._run(ledger=self.dup_urls)
            self.assertNotIn(self.dup_urls[0] + ".md", [u for u, _ in calls])
        finally:
            self.dup_urls = ()

    def test_short_page_ends_pagination(self):
        # 2 listings < 24 per page → page 2 is never requested
        _, _, _, calls = self._run(max_pages=3)
        pages = [p["page"] for u, p in calls if u.endswith("/jobs.md")]
        self.assertEqual(pages, [1])


if __name__ == "__main__":
    unittest.main()
