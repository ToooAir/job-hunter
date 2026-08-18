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
        resolve_bundesagentur,
        resolve_greenhouse,
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
            # extensionless asset path — greenhouse's board loader script
            "https://boards.greenhouse.io/embed/job_board/js?for=fundedclub",
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

    def test_greenhouse_embed_loader_is_repaired_not_dropped(self):
        loader = "https://boards.greenhouse.io/embed/job_board/js?for=fundedclub"
        self.assertEqual(
            _evidence_to_apply_url(loader, "https://funded.club/jobs.html?gh_jid=7665743003"),
            "https://job-boards.greenhouse.io/embed/job_app"
            "?for=fundedclub&token=7665743003")
        # no posting id to recover → the board page, never the .js loader
        self.assertEqual(
            _evidence_to_apply_url(loader, "https://funded.club/jobs.html"),
            "https://job-boards.greenhouse.io/embed/job_board?for=fundedclub")


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



@unittest.skipUnless(HAS_DEPS, "ats_scan deps not installed on this host")
class ResolveGreenhouseTest(unittest.TestCase):
    """An embedded Greenhouse board serves the same 200 full-board shell for a
    live and a dead posting, so liveness has to come from the board API, whose
    listing contains exactly the OPEN jobs."""

    EMBEDDED = {"id": "j1", "source": "greenhouse", "company": "fundedclub",
                "title": "T", "fit_grade": "B", "match_score": 70,
                "url": "https://funded.club/jobs.html?gh_jid=7665743003",
                "apply_url": None}
    HOSTED = {"id": "j2", "source": "arbeitnow", "company": "Cresta",
              "title": "T", "fit_grade": "A", "match_score": 80,
              "url": "https://job-boards.greenhouse.io/cresta/jobs/4668107008",
              "apply_url": None}

    def setUp(self):
        import ats_scan
        ats_scan._gh_open_postings.cache_clear()

    def _resolve(self, job, status=200, payload=None):
        from unittest import mock
        fake = mock.Mock(status_code=status)
        fake.json.return_value = payload if payload is not None else {}
        result = {"ats": "unknown", "evidence": ""}
        with mock.patch("ats_scan.requests.get", return_value=fake) as get:
            handled = resolve_greenhouse(dict(job), result)
        return handled, result, get

    @staticmethod
    def _board(*jobs):
        return {"jobs": list(jobs), "meta": {"total": len(jobs)}}

    def test_posting_absent_from_the_board_is_gone(self):
        handled, res, _ = self._resolve(
            self.EMBEDDED, payload=self._board({"id": 7818931003, "absolute_url": "x"}))
        self.assertTrue(handled)
        self.assertEqual(res["ats"], "gone")
        self.assertIn("7665743003", res["evidence"])

    def test_live_embedded_posting_gets_the_embed_form(self):
        # absolute_url on an embedded board points back at the JS-only host page
        _, res, _ = self._resolve(self.EMBEDDED, payload=self._board(
            {"id": 7665743003,
             "absolute_url": "https://funded.club/jobs.html?gh_jid=7665743003"}))
        self.assertEqual(res["ats"], "greenhouse")
        self.assertEqual(
            res["evidence"],
            "https://job-boards.greenhouse.io/embed/job_app"
            "?for=fundedclub&token=7665743003")

    def test_live_hosted_posting_keeps_its_own_url(self):
        absolute = "https://job-boards.greenhouse.io/cresta/jobs/4668107008"
        _, res, _ = self._resolve(
            self.HOSTED, payload=self._board({"id": 4668107008, "absolute_url": absolute}))
        self.assertEqual(res["ats"], "greenhouse")
        self.assertEqual(res["evidence"], absolute)

    def test_unreadable_board_never_condemns_the_posting(self):
        # a wrong slug (404) or a flaky API must fall through, not mark gone
        for status in (404, 500):
            handled, res, _ = self._resolve(self.EMBEDDED, status=status)
            self.assertFalse(handled, status)
            self.assertEqual(res["ats"], "unknown")

    def test_truncated_board_response_is_refused(self):
        # meta.total > len(jobs) → judging absence would invent takedowns
        handled, res, _ = self._resolve(self.EMBEDDED, payload={
            "jobs": [{"id": 1, "absolute_url": "x"}], "meta": {"total": 400}})
        self.assertFalse(handled)
        self.assertEqual(res["ats"], "unknown")

    def test_board_is_fetched_once_across_jobs(self):
        from unittest import mock
        fake = mock.Mock(status_code=200)
        fake.json.return_value = self._board({"id": 7665743003, "absolute_url": "x"})
        with mock.patch("ats_scan.requests.get", return_value=fake) as get:
            for _ in range(3):
                resolve_greenhouse(dict(self.EMBEDDED), {"ats": "unknown", "evidence": ""})
            self.assertEqual(get.call_count, 1)

    def test_slug_from_a_resolved_apply_url(self):
        # aggregator-sourced job: company is a display name, not a board slug
        job = {"id": "j3", "source": "arbeitnow", "company": "Moonfare", "title": "T",
               "fit_grade": "B", "match_score": 70,
               "url": "https://www.arbeitnow.com/jobs/companies/moonfare/data-engineer",
               "apply_url": "https://job-boards.greenhouse.io/embed/job_app"
                            "?for=moonfare&token=7773348003"}
        _, res, get = self._resolve(job, payload=self._board({"id": 7773348003}))
        self.assertIn("boards/moonfare/jobs", get.call_args[0][0])
        self.assertEqual(res["ats"], "greenhouse")

    def test_non_greenhouse_job_is_not_handled(self):
        job = {"id": "j4", "source": "heise", "company": "Acme", "title": "T",
               "fit_grade": "B", "match_score": 70,
               "url": "https://jobs.heise.de/job?id=123", "apply_url": None}
        handled, _, get = self._resolve(job)
        self.assertFalse(handled)
        get.assert_not_called()


@unittest.skipUnless(HAS_DEPS, "ats_scan deps not installed on this host")
class ResolveBundesagenturTest(unittest.TestCase):
    """The BA job page is a client-rendered SPA: a withdrawn posting and a live
    one return the same 200 shell. 68% of BA rows are walled (apply_url == url),
    so there is no downstream ATS page to check instead — the detail API is the
    only witness."""

    WALLED = {"id": "b1", "source": "bundesagentur", "company": "Acme", "title": "T",
              "fit_grade": "B", "match_score": 70,
              "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/13644-299674-S",
              "apply_url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/13644-299674-S"}
    EXTERNAL = {**WALLED, "id": "b2",
                "apply_url": "https://jobs.lever.co/acme/45b0fae3/apply"}

    _UNSET = object()

    def _resolve(self, job, status=200, payload=_UNSET, body=b"{}"):
        from unittest import mock
        fake = mock.Mock(status_code=status, content=body)
        if payload is self._UNSET:
            fake.json.return_value = {"stellenangebotsTitel": "T"}
        elif payload is None:
            fake.json.side_effect = ValueError("not json")  # HTML maintenance page
        else:
            fake.json.return_value = payload
        result = {"ats": "unknown", "evidence": ""}
        with mock.patch("ats_scan.requests.get", return_value=fake) as get:
            handled = resolve_bundesagentur(dict(job), result)
        return handled, result, get

    def test_404_marks_the_posting_gone(self):
        handled, res, _ = self._resolve(self.WALLED, status=404)
        self.assertTrue(handled)
        self.assertEqual(res["ats"], "gone")
        self.assertIn("13644-299674-S", res["evidence"])

    def test_the_detail_call_is_keyed_on_the_base64_refnr(self):
        import base64
        _, _, get = self._resolve(self.WALLED)
        self.assertIn(base64.b64encode(b"13644-299674-S").decode(), get.call_args[0][0])

    def test_a_live_walled_posting_keeps_the_ba_page_as_the_channel(self):
        _, res, _ = self._resolve(self.WALLED)
        self.assertEqual(res["ats"], "unknown")
        self.assertEqual(res["evidence"], self.WALLED["url"])

    def test_a_live_posting_with_an_external_link_is_classified(self):
        _, res, _ = self._resolve(self.EXTERNAL)
        self.assertEqual(res["ats"], "lever")
        self.assertEqual(res["evidence"], self.EXTERNAL["apply_url"])

    def test_the_maintenance_page_is_never_read_as_a_takedown(self):
        # HTML under HTTP 200 — the failure mode that hid this source's death
        # for four months. Must fall through to the generic path, not judge.
        handled, res, _ = self._resolve(self.WALLED, payload=None,
                                        body=b"<html>Wartungsarbeiten</html>")
        self.assertFalse(handled)
        self.assertNotEqual(res["ats"], "gone")

    def test_5xx_is_fetch_error_not_gone(self):
        _, res, _ = self._resolve(self.WALLED, status=503)
        self.assertEqual(res["ats"], "fetch-error")

    def test_a_non_ba_url_is_not_handled(self):
        job = {**self.WALLED, "url": "https://jobs.heise.de/job?id=1"}
        handled, _, get = self._resolve(job)
        self.assertFalse(handled)
        get.assert_not_called()

if __name__ == "__main__":
    unittest.main()
