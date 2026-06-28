"""Tests for ats_scan.py pure helpers — apply-URL plausibility filter.

Regression for junk apply_url evidence: ATS-domain matches anywhere in the
HTML (script srcs, footer terms links) prove the ATS but are not apply links.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # ats_scan needs requests/bs4 — present in the container, not the host
    from ats_scan import _evidence_to_apply_url, plausible_apply_url
    HAS_DEPS = True
except ModuleNotFoundError:
    HAS_DEPS = False


@unittest.skipUnless(HAS_DEPS, "ats_scan deps not installed on this host")
class PlausibleApplyUrlTest(unittest.TestCase):
    def test_real_apply_pages_pass(self):
        for url in (
            "https://example.jobs.personio.de/job/745388?apply",
            "https://jobs.lever.co/example/45b0fae3/apply",
            "https://join.com/companies/example/123-engineer",
            "https://de.indeed.com/viewjob?jk=743e9c8399597221",
            "mailto:jobs@example.de",
        ):
            self.assertTrue(plausible_apply_url(url), url)

    def test_static_assets_rejected(self):
        for url in (
            "https://performancemanager5.successfactors.eu/verp/vmod_v1/"
            "ui/extlib/jquery_3.5.1/jquery.js",
            "https://example.com/assets/app.css",
            "https://example.com/logo.svg",
        ):
            self.assertFalse(plausible_apply_url(url), url)

    def test_terms_and_privacy_pages_rejected(self):
        for url in (
            "https://join.com/terms",
            "https://example.com/privacy-policy",
            "https://example.de/impressum",
            "https://example.de/datenschutz?lang=de",
        ):
            self.assertFalse(plausible_apply_url(url), url)

    def test_bare_and_locale_homepages_rejected(self):
        for url in (
            "https://www.heyjobs.co/",
            "https://www.heyjobs.co",
            "https://www.heyjobs.co/de-de",
            "https://example.com/en",
        ):
            self.assertFalse(plausible_apply_url(url), url)

    def test_non_http_rejected(self):
        for url in ("", None, "javascript:void(0)", "ftp://x/apply"):
            self.assertFalse(plausible_apply_url(url), url)

    def test_evidence_to_apply_url_filters(self):
        self.assertIsNone(_evidence_to_apply_url("https://join.com/terms"))
        self.assertEqual(
            _evidence_to_apply_url("  https://join.com/companies/x/1-dev  "),
            "https://join.com/companies/x/1-dev")
        self.assertIsNone(_evidence_to_apply_url("native form, 4 schema fields"))


if __name__ == "__main__":
    unittest.main()
