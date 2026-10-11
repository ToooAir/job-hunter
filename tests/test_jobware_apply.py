"""Tests for the jobware apply-route extraction (2026-09-13).

jobware bounces every anonymous view of /job/<slug> to
/account/login?redirect=…, so the job page is unreadable to the pipeline. The
listing API we already call at ingest carries the real route per posting — this
is the code that reads it.

phase1_ingestor imports requests/yaml at module level — run inside the container:
    docker exec job-hunter-pipeline-1 python3 -m unittest tests.test_jobware_apply -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import phase1_ingestor as ing  # noqa: E402

# The real interstitial is ~750 bytes and carries the target twice.
WEITERLEITUNG = (
    '<html><head><title>Weiterleitung</title>'
    '<meta http-equiv="refresh" content="5;URL=https://careers.example.com/'
    'offer-api/?offerApiId=215681-de_DE&utm_source=bfa"/></head><body>'
    '<script>location.href = "https://careers.example.com/offer-api/'
    '?offerApiId=215681-de_DE&utm_source=bfa"</script></body></html>'
)


def _resp(text, status=200):
    r = mock.Mock()
    r.text = text
    r.status_code = status
    r.raise_for_status = mock.Mock()
    return r


class JobwareApplyUrlTest(unittest.TestCase):
    def test_email_client_yields_the_mailto(self):
        # 14 of 16 postings in the sample: the employer takes applications by
        # mail and jobware just relays them
        job = {"apply": {"type": "EMAIL_CLIENT",
                         "url": "mailto:bewerbung@aconium.eu",
                         "b2gUrl": "https://www.jobware.de/apply/tok"}}
        self.assertEqual(ing.jw_apply_url(job), "mailto:bewerbung@aconium.eu")

    def test_anzeige_has_no_link(self):
        # apply instructions live in the advert text — inventing a link here
        # would send the human somewhere jobware never pointed
        self.assertIsNone(ing.jw_apply_url({"apply": {"type": "ANZEIGE"}}))

    def test_missing_apply_object_is_none(self):
        self.assertIsNone(ing.jw_apply_url({}))

    def test_extern_is_followed_to_the_employers_ats(self):
        # the point of resolving at ingest: classify_url can only recognise a
        # posting once the jobware token is out of the way
        job = {"apply": {"type": "EXTERN",
                         "url": "https://www.jobware.de/apply/tok"}}
        with mock.patch.object(ing.requests, "get",
                               return_value=_resp(WEITERLEITUNG)) as g:
            self.assertEqual(
                ing.jw_apply_url(job),
                "https://careers.example.com/offer-api/"
                "?offerApiId=215681-de_DE&utm_source=bfa")
        self.assertEqual(g.call_args[0][0], "https://www.jobware.de/apply/tok")

    def test_extern_falls_back_to_the_token_url_when_unreachable(self):
        # a dead interstitial must not lose the posting: the token URL still
        # works in a browser, it just tells classify_url nothing
        job = {"apply": {"type": "EXTERN",
                         "url": "https://www.jobware.de/apply/tok"}}
        with mock.patch.object(ing.requests, "get",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(ing.jw_apply_url(job),
                             "https://www.jobware.de/apply/tok")

    def test_extern_without_a_redirect_target_keeps_the_token_url(self):
        job = {"apply": {"type": "EXTERN",
                         "url": "https://www.jobware.de/apply/tok"}}
        with mock.patch.object(ing.requests, "get",
                               return_value=_resp("<html>nothing here</html>")):
            self.assertEqual(ing.jw_apply_url(job),
                             "https://www.jobware.de/apply/tok")

    def test_html_accept_header_not_the_json_one(self):
        # JW_HEADERS asks for application/json (the listing API); the
        # interstitial is HTML and answers 406-ish shapes to that
        job = {"apply": {"type": "EXTERN",
                         "url": "https://www.jobware.de/apply/tok"}}
        with mock.patch.object(ing.requests, "get",
                               return_value=_resp(WEITERLEITUNG)) as g:
            ing.jw_apply_url(job)
        self.assertIn("text/html", g.call_args[1]["headers"]["Accept"])


if __name__ == "__main__":
    unittest.main()


class JobwareJdSourceTest(unittest.TestCase):
    """The listing API's `task` field is the Aufgaben half and nothing else —
    there is no requirements field in that payload. Scoring it alone hid every
    knock-out criterion: measured 2026-10-11, 531 jobware rows outscored their
    own full-text twin from another source, 211 of those twins had been zeroed
    by the German-language gate, and 11 were applied to. Real example: Schmitt
    GmbH's task was 554B with no "Go" and no "Deutsch"; the iframe was 2,380B
    and carried "Sehr gute Kenntnisse in Go (Golang)" and "Sehr gute Deutsch-
    und Englischkenntnisse".
    """

    TASK = "- Entwicklung von Backend-Komponenten\n- Aufbau von CI/CD-Pipelines"
    IFRAME = (TASK + "\nIhr Profil:\n- Sehr gute Kenntnisse in Go (Golang) und Python"
                     "\n- Sehr gute Deutsch- und Englischkenntnisse in Wort und Schrift")

    def _ingest(self, job_extra):
        job = {"id": "2065370335",
               "url": "software-engineer-m-w-d-064576288", "title": "Software Engineer (m/w/d)",
               "location": "Erlangen (DE)", "advertiser": {"name": "Schmitt GmbH"},
               "apply": {"type": "EMAIL_CLIENT", "url": "mailto:x@schmitt.de"},
               **job_extra}
        captured = []
        with mock.patch.object(ing, "_jw_safe_get", return_value=[job]), \
             mock.patch.object(ing, "_jw_iframe_jd", side_effect=lambda u: self.IFRAME if u else ""), \
             mock.patch.object(ing, "_url_in_db", return_value=False), \
             mock.patch.object(ing, "time"), \
             mock.patch.object(ing, "upsert_job",
                               side_effect=lambda c, rec: captured.append(rec) or True):
            ing.scrape_jobware(conn=mock.Mock(), keywords=["Software Engineer"],
                               locations=["Erlangen"], radius_km=50, include_remote=False)
        return captured

    def test_the_knockout_criteria_reach_the_record(self):
        rec = self._ingest({"task": self.TASK,
                            "resourceUrlWithTracking": "https://www.jobware.de/embed/1"})[0]
        self.assertIn("Golang", rec["raw_jd_text"])
        self.assertIn("Deutsch- und Englischkenntnisse", rec["raw_jd_text"])

    def test_task_is_the_fallback_not_the_source(self):
        """No iframe URL → the half-advert is better than nothing, but it must
        not be what we take while an iframe is on offer."""
        rec = self._ingest({"task": self.TASK, "resourceUrlWithTracking": ""})[0]
        self.assertEqual(rec["raw_jd_text"], self.TASK)
