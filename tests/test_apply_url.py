"""Tests for utils/apply_url.py — the shared "could a human apply here?" gate.

Extracted from ats_scan (which still re-exports plausible_apply_url, so its own
tests keep covering the ats_scan seam) so phase1's bundesagentur scraper can
use it without pulling in requests/bs4.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_url import account_wall_url, plausible_apply_url  # noqa: E402


class PlausibleApplyUrlTest(unittest.TestCase):
    def test_real_apply_pages_pass(self):
        for url in (
            "https://example.jobs.personio.de/job/745388?apply",
            "https://jobs.lever.co/example/45b0fae3/apply",
            "https://www.get-in-it.de/jobsuche/stelle/12345",
            "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003259961-S",
            "mailto:jobs@example.de",
        ):
            self.assertTrue(plausible_apply_url(url), url)

    def test_bare_hosts_and_homepages_rejected(self):
        for url in (
            "www.zalando.de",            # bundesagentur's externeURL shape
            "www.plusyou.de",
            "http://www.arbeitsagentur.de",
            "https://example.com/",
            "https://example.com/de-de",
        ):
            self.assertFalse(plausible_apply_url(url), url)

    def test_a_bare_path_with_an_identifying_query_is_a_deep_link(self):
        # compleet publishes real postings this way; the path alone is "/"
        self.assertTrue(plausible_apply_url(
            "https://jobboard.compleet.com/?externalId=4911674827"))
        self.assertTrue(plausible_apply_url("https://example.com/de-de?jobid=7"))
        # …but tracking params identify the referrer, not the job
        self.assertFalse(plausible_apply_url("https://example.com/?utm_source=ba"))
        self.assertFalse(plausible_apply_url("https://example.com/?ref=partner"))

    def test_assets_and_boilerplate_rejected(self):
        for url in (
            "https://example.com/assets/app.css",
            "https://boards.greenhouse.io/embed/job_board/js?for=acme",
            "https://join.com/terms",
            "https://example.de/impressum",
        ):
            self.assertFalse(plausible_apply_url(url), url)

    def test_account_walls_are_not_apply_links(self):
        # jobware bounces anonymous job views here and the page really does
        # render E-Mail/Vorname/Nachname/Telefonnummer — it looked like a form
        # and got stored as the apply link for five rows (2026-09-13)
        for url in (
            "https://www.jobware.de/account/login?redirect=%2Fjob%2Fdata-eng.html",
            "https://example.com/login",
            "https://example.de/anmelden",
            "https://example.com/users/sign-up",
            "https://example.de/registrierung/",
        ):
            self.assertTrue(account_wall_url(url), url)
            self.assertFalse(plausible_apply_url(url), url)

    def test_a_posting_whose_slug_says_login_is_not_a_wall(self):
        # the wall lives in a path SEGMENT; "Login Engineer" is a job title
        for url in (
            "https://example.com/de/jobs/login-engineer-m-w-d.123.html",
            "https://example.com/jobs/single-sign-on-specialist",
        ):
            self.assertFalse(account_wall_url(url), url)
            self.assertTrue(plausible_apply_url(url), url)

    def test_mailto_survives_the_wall_check(self):
        # 14 of 16 jobware postings apply by mail
        self.assertTrue(plausible_apply_url("mailto:bewerbung@aconium.eu"))

    def test_non_http_rejected(self):
        for url in ("", None, "javascript:void(0)", "ftp://x/apply"):
            self.assertFalse(plausible_apply_url(url), url)


if __name__ == "__main__":
    unittest.main()
