"""scripts/skill_gaps.py aggregation: per-grade demand, case-merged names."""

import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.skill_gaps import aggregate, render  # noqa: E402


class AggregateTest(unittest.TestCase):
    def test_counts_per_grade_and_merges_case(self):
        rows = [{"id": "1", "fit_grade": "A"}, {"id": "2", "fit_grade": "C"},
                {"id": "3", "fit_grade": "C"}, {"id": "4", "fit_grade": "B"}]
        ex = {"1": {"required": ["Python", "AWS"], "nice": []},
              "2": {"required": ["python", "Kubernetes"], "nice": ["AWS"]},
              "3": {"required": ["Kubernetes", "Kubernetes"], "nice": ["Kubernetes"]}}
        by = {s["skill"].casefold(): s for s in aggregate(rows, ex)}
        self.assertEqual((by["python"]["A"], by["python"]["C"]), (1, 1))
        # required wins over nice in the same posting, duplicates count once
        self.assertEqual((by["kubernetes"]["C"], by["kubernetes"]["nice"]), (2, 0))
        self.assertEqual((by["aws"]["A"], by["aws"]["nice"], by["aws"]["demand"]), (1, 1, 1.5))

    def test_render_ranks_missing_above_covered(self):
        skills = [{"skill": "Python", "A": 2, "B": 0, "C": 0, "nice": 0, "demand": 2},
                  {"skill": "Kubernetes", "A": 1, "B": 0, "C": 0, "nice": 0, "demand": 1}]
        md = render(skills, {"Python": {"level": "strong"}, "Kubernetes": {"level": "none"}},
                    [], 2, Counter({"A": 2}))
        gaps = md.split("## Biggest gaps")[1].split("## Learning plan")[0]
        self.assertIn("Kubernetes", gaps)
        self.assertNotIn("Python", gaps)


if __name__ == "__main__":
    unittest.main()
