"""Tests for the seniority-overreach penalty regex in phase2_scorer.

Guards the false-positive boundary calibrated against the live pool: the penalty
must fire on Principal/Staff/Head-of/Architect titles but leave the candidate's
in-range roles (Senior, Lead, plain Engineer) and finance-inflated VP titles alone.

Needs the LLM SDK stack installed (phase2_scorer imports openai at module level):
    docker exec job-hunter-pipeline-1 python3 -m unittest tests.test_seniority_overreach -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase2_scorer import SENIORITY_OVERREACH_RE  # noqa: E402


def matches(title: str) -> bool:
    return bool(SENIORITY_OVERREACH_RE.search(title))


class TestSeniorityOverreach(unittest.TestCase):
    def test_overreach_titles_fire(self):
        for t in [
            "Principal Software Engineer",
            "Principal Full-Stack Engineer (m/f/x)",
            "Staff Backend Engineer",
            "Staff / Principal Backend Engineer (Python)",
            "Senior Staff Machine Learning Engineer",
            "Head of Engineering",
            "Head of Software Engineering (m/w/d)",
            "Senior Python Cloud Architect",
            "Lead AI Architect (d/f/m)",
        ]:
            self.assertTrue(matches(t), f"should penalise: {t}")

    def test_in_range_titles_do_not_fire(self):
        # Senior and Lead are in the candidate's correct range (4 yrs + tech lead) — never penalise.
        for t in [
            "Senior Backend Engineer (m/f/d)",
            "Senior Software Engineer (Python/Web Dev)",
            "Software Engineer",
            "Backend Developer",
            "Lead Backend Engineer",
            "Tech Lead",
            "AI/ML Engineer",
        ]:
            self.assertFalse(matches(t), f"should NOT penalise: {t}")

    def test_finance_vp_and_director_are_left_alone(self):
        # Deliberately OUT of the regex: bank title inflation makes these IC roles,
        # and they are low-frequency — penalising them would be a false positive.
        for t in [
            "Senior Backend Python Engineer - AI Platform, Vice President",
            "Technology Data Solutions Engineer - Assistant Vice President",
            "Director Engineering [m/w/d] Backend - Hamburg (Hybrid)",
        ]:
            self.assertFalse(matches(t), f"should NOT penalise (out of scope): {t}")

    def test_word_boundary_no_substring_false_positives(self):
        # "staff" must not leak into unrelated words; "architect" must be the noun.
        self.assertFalse(matches("Staffing Coordinator (non-eng)"))
        self.assertFalse(matches("Engineer, Software Architecture Enablement Team"))


if __name__ == "__main__":
    unittest.main()
