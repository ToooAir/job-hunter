"""Tests for utils.lang_req.german_required (pre-LLM de_required gate)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.lang_req import german_required  # noqa: E402


class TestGermanRequired(unittest.TestCase):
    def test_hard_german_phrases_hit(self):
        for s in [
            "Sehr gute Deutschkenntnisse in Wort und Schrift (C1)",
            "Verhandlungssichere Deutschkenntnisse sowie gute Englischkenntnisse",
            "Fließende Deutschkenntnisse sind Voraussetzung",
            "Deutsch auf muttersprachlichem Niveau",
            "Du sprichst fließend Deutsch und Englisch",
            "Fluent German (C1 or above) is required",
            "German at native level",
            "Fluent German is mandatory",
        ]:
            self.assertIsNotNone(german_required(s), s)

    def test_english_level_is_not_german(self):
        # the 23-row trap: bare C1 next to English
        for s in [
            "English C1 required. German is a plus.",
            "Language: English (C1), Spanish (B2)",
            "This role is remote #LI-Remote #C1 #USEAST",
            "Fluent in English; German B1 desirable",
            "Sehr gute Englisch- und gute Deutschkenntnisse",
            "Full-stack developer (React Native) based in Germany",
            "German language proficiency (B1 or above), useful for collaboration",
        ]:
            self.assertIsNone(german_required(s), s)

    def test_softened_requirement_is_left_to_the_llm(self):
        for s in [
            "Fließende Deutschkenntnisse von Vorteil",
            "Fluent German is a plus but not required",
            "Sehr gute Deutschkenntnisse wünschenswert",
            "Fluent in English or German",
            "German C1 preferred, English fluent",
            "Fließend Deutsch oder Englisch",
        ]:
            self.assertIsNone(german_required(s), s)

    def test_b2_and_lower_are_not_hard(self):
        self.assertIsNone(german_required("Deutschkenntnisse mind. B2"))
        self.assertIsNone(german_required("German B1 or better"))

    def test_empty(self):
        self.assertIsNone(german_required(""))
        self.assertIsNone(german_required(None))

    def test_returns_the_phrase(self):
        hit = german_required("Wir erwarten verhandlungssichere Deutschkenntnisse.")
        self.assertIn("verhandlungssichere Deutschkenntnisse", hit)


if __name__ == "__main__":
    unittest.main()
