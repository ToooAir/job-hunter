"""Tests for utils/apply_llm.py (Step 4.2) — fake client, no network.

Fixtures use fictional Max Mustermann data only.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_llm import (  # noqa: E402
    MAX_ANSWER_WORDS,
    answer_open_questions,
    build_profile_facts,
    map_pending_fields,
)
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

JOB = {"title": "Backend Engineer", "company": "Mustermann GmbH"}


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


def fld(**kw):
    base = {"selector": '[name="x"]', "kind": "text", "label": "",
            "reason": "no-deterministic-match"}
    base.update(kw)
    return base


def respond(rows):
    return json.dumps({"fields": rows})


class TestMapPendingFields(unittest.TestCase):
    def test_no_pending_makes_no_call(self):
        client = FakeClient([])  # raises if called
        out = map_pending_fields([], PROFILE, JOB, client=client, model="m")
        self.assertEqual(out["actions"], [])

    def test_cover_letter_slot_bypasses_llm(self):
        client = FakeClient([])
        out = map_pending_fields(
            [fld(label="Anschreiben", reason="cover-letter-slot")],
            PROFILE, JOB, client=client, model="m")
        self.assertEqual(len(out["cover_letter_slots"]), 1)
        self.assertEqual(client.calls, [])

    def test_value_decision_becomes_reviewed_action(self):
        client = FakeClient([respond(
            [{"index": 0, "decision": "value", "value": "5 years"}])])
        out = map_pending_fields(
            [fld(label="Years of Experience")], PROFILE, JOB,
            client=client, model="m")
        a = out["actions"][0]
        self.assertEqual((a["value"], a["source"], a["needs_review"]),
                         ("5 years", "llm", True))

    def test_select_value_must_be_verbatim_option(self):
        client = FakeClient([respond(
            [{"index": 0, "decision": "value", "value": "Five years"}])])
        out = map_pending_fields(
            [fld(kind="select", label="Erfahrung", options=["<1", "1-3", "3-5", ">5"])],
            PROFILE, JOB, client=client, model="m")
        self.assertFalse(out["actions"])
        self.assertEqual(out["unfilled"][0]["reason"], "llm-option-mismatch")

    def test_checkbox_check_value(self):
        client = FakeClient([respond(
            [{"index": 0, "decision": "value", "value": "check"}])])
        out = map_pending_fields(
            [fld(kind="checkbox", label="Ich bin sofort verfügbar")],
            PROFILE, JOB, client=client, model="m")
        self.assertEqual(out["actions"][0]["action"], "check")

    def test_open_question_routed_with_selector(self):
        client = FakeClient([respond(
            [{"index": 0, "decision": "open_question"}])])
        out = map_pending_fields(
            [fld(kind="textarea", label="Warum wir?", selector="#why")],
            PROFILE, JOB, client=client, model="m")
        q = out["open_questions"][0]
        self.assertEqual((q["selector"], q["question"]), ("#why", "Warum wir?"))

    def test_skip_and_needs_human_become_unfilled(self):
        client = FakeClient([respond([
            {"index": 0, "decision": "skip", "reason": "newsletter"},
            {"index": 1, "decision": "needs_human", "reason": "legal"},
        ])])
        out = map_pending_fields(
            [fld(label="Newsletter"), fld(label="Wehrdienst geleistet?")],
            PROFILE, JOB, client=client, model="m")
        reasons = [u["reason"] for u in out["unfilled"]]
        self.assertEqual(reasons, ["llm-skip: newsletter", "needs-human: legal"])

    def test_unaddressed_fields_never_silently_dropped(self):
        client = FakeClient([respond(
            [{"index": 0, "decision": "value", "value": "x"}])])
        out = map_pending_fields(
            [fld(label="A"), fld(label="B")], PROFILE, JOB,
            client=client, model="m")
        self.assertEqual(out["unfilled"][0]["reason"], "llm-unaddressed")

    def test_malformed_json_twice_degrades_to_unfilled(self):
        client = FakeClient(["not json", "{still broken"])
        out = map_pending_fields(
            [fld(label="A", required=True)], PROFILE, JOB,
            client=client, model="m")
        u = out["unfilled"][0]
        self.assertEqual(u["reason"], "llm-error")
        self.assertTrue(u["required"])
        self.assertEqual(len(client.calls), 2)

    def test_accounting_every_field_lands_once(self):
        client = FakeClient([respond([
            {"index": 0, "decision": "value", "value": "v"},
            {"index": 1, "decision": "open_question"},
            {"index": 2, "decision": "skip"},
        ])])
        pending = [fld(label="A"), fld(label="B"), fld(label="C"), fld(label="D"),
                   fld(label="CL", reason="cover-letter-slot")]
        out = map_pending_fields(pending, PROFILE, JOB, client=client, model="m")
        total = (len(out["actions"]) + len(out["open_questions"])
                 + len(out["unfilled"]) + len(out["cover_letter_slots"]))
        self.assertEqual(total, len(pending))

    def test_prompt_contains_facts_and_sanitized_labels(self):
        client = FakeClient([respond([])])
        map_pending_fields(
            [fld(label="<script>Jahre</script>")], PROFILE, JOB,
            client=client, model="m")
        user_msg = client.calls[0]["messages"][1]["content"]
        self.assertIn("first_name: Max", user_msg)
        self.assertIn("Already holds a German residence permit", user_msg)
        self.assertNotIn("<script>", user_msg)


class TestAnswerOpenQuestions(unittest.TestCase):
    def test_no_questions_no_call(self):
        out = answer_open_questions([], JOB, "", client=FakeClient([]), model="m")
        self.assertEqual(out["custom_qa"], [])

    def test_answer_yields_action_and_qa_record(self):
        client = FakeClient([json.dumps(
            {"answers": [{"index": 0, "answer": "I built RAG pipelines."}]})])
        out = answer_open_questions(
            [fld(kind="textarea", selector="#why", question="Why us?")],
            JOB, "KB context here", client=client, model="m")
        self.assertEqual(out["actions"][0]["value"], "I built RAG pipelines.")
        self.assertTrue(out["actions"][0]["needs_review"])
        self.assertEqual(out["custom_qa"][0]["question"], "Why us?")
        self.assertIn("KB context here", client.calls[0]["messages"][1]["content"])

    def test_overlong_answer_truncated(self):
        long_answer = " ".join(["word"] * (MAX_ANSWER_WORDS + 50))
        client = FakeClient([json.dumps(
            {"answers": [{"index": 0, "answer": long_answer}]})])
        out = answer_open_questions(
            [fld(question="Q?")], JOB, "", client=client, model="m")
        self.assertLessEqual(len(out["actions"][0]["value"].split()),
                             MAX_ANSWER_WORDS + 1)

    def test_unanswered_question_surfaces_as_unfilled(self):
        client = FakeClient([json.dumps({"answers": []})])
        out = answer_open_questions(
            [fld(question="Q?", required=True)], JOB, "", client=client, model="m")
        self.assertEqual(out["unfilled"][0]["reason"], "llm-error")


class TestProfileFacts(unittest.TestCase):
    def test_facts_include_explanations(self):
        facts = build_profile_facts(PROFILE)
        self.assertIn("requires_sponsorship: No (note: Already holds", facts)


if __name__ == "__main__":
    unittest.main()
