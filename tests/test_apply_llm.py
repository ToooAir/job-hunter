"""Tests for utils/apply_llm.py — fake client, no network.

Also home of FakeClient, the LLM test double the graph/verifier tests import.
The mapping/answering tests left with their functions (mapping chain retired
2026-07-02). Fixtures use fictional Max Mustermann data only.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_llm import build_profile_facts  # noqa: E402
from utils.profile_loader import CandidateProfile  # noqa: E402

PROFILE = CandidateProfile({
    "meta": {"cv_path": "candidate_kb/cv/cv.pdf"},
    "fields": {
        "first_name": {"value": "Max", "aliases": ["vorname"]},
        "requires_sponsorship": {
            "value": "No", "aliases": ["sponsorship"],
            "explanation": "Already holds a German residence permit.",
        },
    },
})


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeClient:
    """Returns queued response strings; records every request payload."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls.append(kw)
                if not outer._responses:
                    raise AssertionError("unexpected LLM call")
                return _Resp(outer._responses.pop(0))

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


class TestProfileFacts(unittest.TestCase):
    def test_facts_include_explanations(self):
        facts = build_profile_facts(PROFILE)
        self.assertIn("requires_sponsorship: No (note: Already holds", facts)


if __name__ == "__main__":
    unittest.main()
