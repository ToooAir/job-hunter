"""Tests for salary_estimator prompt assembly — no LLM, no network.

Covers the candidate-positioning injection (2026-07) and, as a wiring guard,
that every prompt placeholder still resolves once a new section is added.

Run:  python -m unittest tests.test_salary_estimator -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import salary_estimator as se  # noqa: E402


class CandidateSectionTest(unittest.TestCase):
    def test_positioning_is_injected_and_framed_as_the_candidate(self):
        out = se._build_candidate_section(
            "4+ years backend engineering, moving into AI/consulting."
        )
        self.assertIn("4+ years backend engineering", out)
        # framed so the model calibrates to the person, not just the role
        self.assertIn("Candidate", out)
        self.assertIn("fit", out.lower())

    def test_empty_or_missing_positioning_adds_nothing(self):
        self.assertEqual(se._build_candidate_section(""), "")
        self.assertEqual(se._build_candidate_section("   "), "")
        self.assertEqual(se._build_candidate_section(None), "")

    def test_todo_stub_is_treated_as_absent(self):
        # the .example ships a TODO stub; it must never reach the LLM
        self.assertEqual(
            se._build_candidate_section('TODO: e.g. "5 years backend..."'), ""
        )


class PromptWiringTest(unittest.TestCase):
    JOB = {
        "company": "Musterfirma GmbH",
        "title": "Backend Engineer",
        "location": "Hamburg",
        "contract_type": "Permanent",
        "source": "test",
        "salary_range": "€60,000–€75,000",
        "raw_jd_text": "We build things.",
    }

    def test_template_resolves_with_candidate_section(self):
        # guards against a stray {placeholder} KeyError when sections change
        section = se._build_candidate_section("Senior backend, cloud-native.")
        prompt = se._assemble_prompt(self.JOB, "en", section, "", "")
        self.assertIn("Senior backend, cloud-native.", prompt)
        self.assertIn("Backend Engineer", prompt)
        self.assertIn("€60,000–€75,000", prompt)  # jd-stated salary flows through

    def test_template_resolves_with_all_sections_empty(self):
        prompt = se._assemble_prompt(self.JOB, "en", "", "", "")
        self.assertIn("Musterfirma GmbH", prompt)

    def test_zh_language_resolves(self):
        prompt = se._assemble_prompt(self.JOB, "zh", "", "", "")
        self.assertIn("薪資估計", prompt)


if __name__ == "__main__":
    unittest.main()
