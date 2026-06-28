"""Tests for utils/agentic_mapper.py — the validation/grounding layer.

A fake LLM client feeds canned rows so we exercise the deterministic guards
(hallucinated ids, kind mismatch, placeholder rejection, never-fill, dedup,
accounting) without a real model. Fictional fixtures only.

Run:  python -m unittest tests.test_agentic_mapper -v
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.agentic_mapper import map_page_agentic  # noqa: E402


class _FakeMessage:
    def __init__(self, content):
        self.message = SimpleNamespace(content=content)


class _FakeClient:
    """Returns a fixed JSON payload for chat.completions.create."""

    def __init__(self, payload: dict):
        self._content = json.dumps(payload)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **_):
        return SimpleNamespace(choices=[_FakeMessage(self._content)])


def _profile(never=()):  # fictional candidate
    fields = {
        "first_name": SimpleNamespace(key="first_name", value="Max", explanation=""),
        "email": SimpleNamespace(key="email", value="max@example.com", explanation=""),
    }
    never_set = {n.casefold() for n in never}
    return SimpleNamespace(
        fields=fields,
        meta={"cv_path": "cv/max_mustermann.pdf"},
        is_never_fill=lambda label: (label or "").casefold() in never_set,
    )


FIELDS = [
    {"selector": "#fn", "kind": "text", "label": "Vorname", "required": True},
    {"selector": "#anrede", "kind": "select", "label": "Anrede",
     "options": ["Bitte wählen", "Herr", "Frau"]},
    {"selector": "#cv", "kind": "file", "label": "Lebenslauf", "accept": ".pdf"},
    {"selector": "#consent", "kind": "checkbox", "label": "Newsletter"},
]
JOB = {"title": "Engineer", "company": "Muster GmbH"}


def _run(payload, profile=None, fields=None):
    return map_page_agentic(fields or FIELDS, profile or _profile(),
                            JOB, client=_FakeClient(payload), model="fake")


class HappyPathTest(unittest.TestCase):
    def test_fill_and_select_grounded(self):
        out = _run({"fields": [
            {"id": 0, "action": "fill", "value": "Max"},
            {"id": 1, "action": "select_option", "value": "Herr"},
            {"id": 2, "action": "upload", "value": "cv"},
            {"id": 3, "action": "skip"},
        ]})
        acts = {a["selector"]: a for a in out["actions"]}
        self.assertEqual(acts["#fn"]["value"], "Max")
        self.assertEqual(acts["#anrede"]["value"], "Herr")
        self.assertEqual(acts["#cv"]["action"], "upload")
        self.assertEqual(acts["#cv"]["value"], "cv/max_mustermann.pdf")
        self.assertTrue(all(a["needs_review"] for a in out["actions"]))
        self.assertTrue(all(a["source"].startswith("agentic") for a in out["actions"]))
        self.assertEqual(out["diagnostics"]["addressed"], 4)


class GuardsTest(unittest.TestCase):
    def test_hallucinated_id_dropped(self):
        out = _run({"fields": [{"id": 99, "action": "fill", "value": "x"}]})
        self.assertEqual(out["diagnostics"]["hallucinated_ids"], [99])
        self.assertEqual(out["actions"], [])

    def test_kind_mismatch_dropped(self):
        # select_option aimed at a text field
        out = _run({"fields": [{"id": 0, "action": "select_option", "value": "Herr"}]})
        self.assertEqual(len(out["diagnostics"]["kind_mismatch"]), 1)
        self.assertEqual(out["actions"], [])

    def test_placeholder_option_rejected(self):
        out = _run({"fields": [{"id": 1, "action": "select_option",
                                "value": "Bitte wählen"}]})
        self.assertEqual(out["actions"], [])
        # placeholder is filtered before grounding, so it reports not-grounded
        diag = out["diagnostics"]
        self.assertTrue(diag["option_not_grounded"] or diag["placeholder_rejected"])

    def test_option_not_in_list_dropped(self):
        out = _run({"fields": [{"id": 1, "action": "select_option",
                                "value": "Mr Nonexistent"}]})
        self.assertEqual(out["actions"], [])
        self.assertEqual(len(out["diagnostics"]["option_not_grounded"]), 1)

    def test_never_fill_blocked(self):
        out = _run({"fields": [{"id": 0, "action": "fill", "value": "Max"}]},
                   profile=_profile(never=("Vorname",)))
        self.assertEqual(out["actions"], [])
        self.assertEqual(out["diagnostics"]["never_fill_blocked"], [0])

    def test_duplicate_id_ignored(self):
        out = _run({"fields": [
            {"id": 0, "action": "fill", "value": "Max"},
            {"id": 0, "action": "fill", "value": "Moritz"},
        ]})
        self.assertEqual(len(out["actions"]), 1)
        self.assertEqual(out["actions"][0]["value"], "Max")
        self.assertEqual(out["diagnostics"]["duplicate_ids"], [0])


class AccountingTest(unittest.TestCase):
    def test_every_field_addressed_or_unfilled(self):
        # LLM only answers field 0; the rest must surface as unfilled
        out = _run({"fields": [{"id": 0, "action": "fill", "value": "Max"}]})
        labels_unfilled = {u["reason"] for u in out["unfilled"]}
        self.assertIn("llm-unaddressed", labels_unfilled)
        addressed_plus_unfilled = out["diagnostics"]["addressed"] + sum(
            1 for u in out["unfilled"] if u["reason"] == "llm-unaddressed")
        self.assertEqual(addressed_plus_unfilled, len(FIELDS))

    def test_llm_error_degrades_to_all_unfilled(self):
        # malformed payload (no "fields") still accounts for every field
        out = _run({"garbage": True})
        self.assertEqual(out["actions"], [])
        self.assertEqual(len(out["unfilled"]), len(FIELDS))


if __name__ == "__main__":
    unittest.main()
