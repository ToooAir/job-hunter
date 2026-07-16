"""Tests for utils/gone_text.py — the shared soft-gone wording detector."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.gone_text import GONE_TEXT_RE, redirect_off_posting, soft_gone  # noqa: E402


class RedirectOffPostingTest(unittest.TestCase):
    def test_listing_redirect_fires(self):
        # germantechjobs sends dead postings to a category listing, not the
        # root — the reviewer found these dead by hand (drafts 132/220/245)
        self.assertTrue(redirect_off_posting(
            "https://germantechjobs.de/jobs/ilexius-GmbH-Software-Developer--Data-Scientist-mfd",
            "https://germantechjobs.de/jobs/Data/all"))

    def test_redirect_keeping_slug_or_host_change_is_fine(self):
        # locale/canonical redirects keep the slug — alive
        self.assertFalse(redirect_off_posting(
            "https://x.com/jobs/senior-backend-engineer",
            "https://x.com/en/jobs/senior-backend-engineer"))
        # login redirect carries the slug in the query — not a takedown
        self.assertFalse(redirect_off_posting(
            "https://x.com/jobs/senior-backend-engineer",
            "https://x.com/login?next=/jobs/senior-backend-engineer"))
        # board handing off to the company ATS is the healthy path
        self.assertFalse(redirect_off_posting(
            "https://board.com/jobs/senior-backend-engineer",
            "https://company.greenhouse.io/apply/123"))
        # short/generic last segments ("/job?id=123") never fire
        self.assertFalse(redirect_off_posting(
            "https://jobs.heise.de/job?id=981144808",
            "https://jobs.heise.de/search"))
        self.assertFalse(redirect_off_posting(
            "https://x.com/jobs/abc", "https://x.com/jobs"))
        self.assertFalse(redirect_off_posting(
            "https://x.com/jobs/senior-backend-engineer", None))


class SoftGoneTest(unittest.TestCase):
    def test_german_phrases_hit(self):
        for phrase in ("Diese Stelle ist nicht mehr verfügbar.",
                       "Die Stelle wurde bereits besetzt.",
                       "Stellenanzeige abgelaufen"):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(soft_gone(f"<html><body>{phrase}</body></html>"))

    def test_english_phrases_hit(self):
        for phrase in ("This job is no longer accepting applications",
                       "The position has been filled",
                       "Sorry, this job has expired"):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(soft_gone(f"<div><p>{phrase}</p></div>"))

    def test_phrase_split_across_tags_still_hits(self):
        self.assertIsNotNone(soft_gone("no <b>longer</b> available"))

    def test_jobg8_dead_link_interstitial_hits(self):
        self.assertIsNotNone(soft_gone(
            "<p>Thank you for your interest in the previous job. Unfortunately"
            " we are unable to return you to your original search.</p>"))

    def test_live_page_is_none(self):
        self.assertIsNone(soft_gone(
            "<h1>Senior Engineer (m/w/d)</h1><p>Jetzt bewerben!</p>"))

    def test_i18n_bundle_in_script_does_not_false_positive(self):
        # Live SPA pages ship translation bundles containing every gone phrase.
        html = ('<h1>Backend Engineer</h1>'
                '<script type="text/javascript">var i18n = {"expired":'
                '"This job is no longer available", "filled":'
                '"position has been filled"};</script>'
                '<style>.gone { content: "nicht mehr verfügbar"; }</style>'
                '<!-- fallback: job has expired -->')
        self.assertIsNone(soft_gone(html))

    def test_empty_and_none_are_none(self):
        self.assertIsNone(soft_gone(None))
        self.assertIsNone(soft_gone(""))

    def test_regex_matches_rendered_text_too(self):
        # utils.browser matches the same regex against page.innerText
        self.assertIsNotNone(GONE_TEXT_RE.search(
            "Leider ist diese Stelle nicht mehr aktiv"))


if __name__ == "__main__":
    unittest.main()
