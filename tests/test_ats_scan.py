"""Tests for ats_scan.py pure helpers — apply-URL plausibility filter.

Regression for junk apply_url evidence: ATS-domain matches anywhere in the
HTML (script srcs, footer terms links) prove the ATS but are not apply links.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # ats_scan needs requests/bs4 — present in the container, not the host
    from ats_scan import (
        _evidence_to_apply_url,
        plausible_apply_url,
        resolve_one,
        resolve_wad,
        scan_text_for_ats,
    )
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


@unittest.skipUnless(HAS_DEPS, "ats_scan deps not installed on this host")
class ScanTextForAtsTest(unittest.TestCase):
    def test_plain_ats_url_found(self):
        ats, ev = scan_text_for_ats(
            '<a href="https://jobs.lever.co/acme/123">Apply</a>')
        self.assertEqual(ats, "lever")
        self.assertEqual(ev, "https://jobs.lever.co/acme/123")

    def test_html_entity_json_blob_does_not_poison_the_url(self):
        # A recruitee page embeds its config as &quot;-encoded JSON; the entity
        # hid the closing quote, so the matched URL swallowed the whole blob
        # (idealo draft #100, 2026-07-08). After unescaping, the URL must stop
        # at the real quote.
        blob = ('{&quot;careersHost&quot;:&quot;https://careers-acme.recruitee.com'
                '&quot;,&quot;appEnv&quot;:&quot;production&quot;,&quot;atsHost'
                '&quot;:&quot;recruitee.com&quot;}')
        ats, ev = scan_text_for_ats(blob)
        self.assertEqual(ats, "recruitee")
        self.assertEqual(ev, "https://careers-acme.recruitee.com")
        # …and a bare careers host root is not persisted as the apply link
        self.assertIsNone(_evidence_to_apply_url(ev))

    def test_escaped_slashes_still_handled(self):
        ats, ev = scan_text_for_ats(
            '{"apply_url":"https:\\/\\/jobs.personio.de\\/acme\\/job\\/42"}')
        self.assertEqual(ats, "personio")
        self.assertIn("jobs.personio.de/acme/job/42", ev)


@unittest.skipUnless(HAS_DEPS, "ats_scan deps not installed on this host")
class ResolveOneRedirectTest(unittest.TestCase):
    """A same-board redirect that drops the posting slug = listing taken down
    (germantechjobs bounces dead jobs to /jobs/<category>/all with HTTP 200 —
    reviewers were finding these dead by hand)."""

    JOB = {"id": "j1", "source": "germantechjobs", "company": "Acme", "title": "T",
           "fit_grade": "A", "match_score": 80,
           "url": "https://germantechjobs.de/jobs/Acme-GmbH-Software-Engineer-mfd"}

    def _resolve(self, final_url, text="<html>some other listing</html>"):
        from unittest import mock
        fake = mock.Mock(status_code=200, url=final_url, text=text)
        with mock.patch("ats_scan.requests.get", return_value=fake):
            return resolve_one(dict(self.JOB))

    def test_listing_redirect_marks_gone(self):
        res = self._resolve("https://germantechjobs.de/jobs/Data/all")
        self.assertEqual(res["ats"], "gone")
        self.assertIn("redirected off the posting", res["evidence"])

    def test_known_ats_landing_wins_over_slug_check(self):
        # cross-host handoff to a recognizable ATS is the healthy path
        res = self._resolve("https://boards.greenhouse.io/acme/jobs/123")
        self.assertEqual(res["ats"], "greenhouse")

    def test_same_url_stays_unknown(self):
        res = self._resolve(self.JOB["url"])
        self.assertNotEqual(res["ats"], "gone")


@unittest.skipUnless(HAS_DEPS, "ats_scan deps not installed on this host")
class ResolveWadTest(unittest.TestCase):
    """WeAreDevelopers /ext/ postings are a client-rendered SPA — a live and an
    expired job return the same 200 shell to a raw GET, so liveness must come
    from the private detail API (404 = gone; 200 exposes the downstream link)."""

    URL = "https://www.wearedevelopers.com/en/jobs/ext/7331120/senior-python-dev"

    def _resolve(self, status, json_data=None, url=None):
        from unittest import mock
        fake = mock.Mock(status_code=status)
        fake.json.return_value = json_data if json_data is not None else {}
        result = {"ats": "unknown", "evidence": ""}
        with mock.patch("ats_scan.requests.get", return_value=fake):
            handled = resolve_wad(url or self.URL, result)
        return handled, result

    def test_api_404_marks_gone(self):
        handled, res = self._resolve(404, {"message": "Job not found"})
        self.assertTrue(handled)
        self.assertEqual(res["ats"], "gone")

    def test_200_known_ats_apply_url_is_classified(self):
        _, res = self._resolve(
            200, {"apply_url": "https://boards.greenhouse.io/acme/jobs/1"})
        self.assertEqual(res["ats"], "greenhouse")
        self.assertEqual(res["evidence"], "https://boards.greenhouse.io/acme/jobs/1")

    def test_200_external_non_ats_apply_url_is_unknown_external(self):
        _, res = self._resolve(200, {"apply_url": "https://uk.jobsora.com/job-47"})
        self.assertEqual(res["ats"], "unknown-external")

    def test_200_without_apply_url_stays_unknown(self):
        _, res = self._resolve(200, {"apply_url": ""})
        self.assertEqual(res["ats"], "unknown")

    def test_non_ext_url_is_not_handled(self):
        # native WAD jobs (/jobs/<id>/ without /ext/) fall through to the HTTP path
        result = {"ats": "unknown", "evidence": ""}
        self.assertFalse(
            resolve_wad("https://www.wearedevelopers.com/en/jobs/123/native-role",
                        result))

    def test_5xx_is_fetch_error_not_gone(self):
        # a transient server error must never be mistaken for a takedown
        _, res = self._resolve(503)
        self.assertEqual(res["ats"], "fetch-error")


if __name__ == "__main__":
    unittest.main()
