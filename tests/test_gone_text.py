"""Tests for utils/gone_text.py — the shared soft-gone wording detector."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.gone_text import GONE_TEXT_RE, soft_gone  # noqa: E402


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
