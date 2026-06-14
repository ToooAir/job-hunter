"""Tests for utils/agentic_serialize.py (offline, fictional fixtures only).

Run:  python -m unittest tests.test_agentic_serialize -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.agentic_serialize import ground, serialize_fields  # noqa: E402

# Synthetic form (fictional data — no real candidate/company values).
FIELDS = [
    {"selector": "#fn", "kind": "text", "label": "Vorname *", "required": True},
    {"selector": "#anrede", "kind": "select", "label": "Anrede",
     "options": ["Bitte wählen", "Herr", "Frau"]},
    {"selector": "#cv", "kind": "file", "label": "Lebenslauf",
     "accept": ".pdf,.docx"},
    {"selector": "#nl", "kind": "custom", "label": "Country picker"},
    {"selector": "#guess", "kind": "text", "label": "Select...",
     "required": True, "label_suspect": True},
    {"selector": "#noname", "kind": "text", "label": "", "name": "field_x"},
]


class SerializeTest(unittest.TestCase):
    def test_numbering_is_zero_based_and_dense(self):
        lines = serialize_fields(FIELDS).splitlines()
        self.assertEqual(len(lines), len(FIELDS))
        for i, line in enumerate(lines):
            self.assertTrue(line.startswith(f"[{i}] "))

    def test_required_options_accept_rendered(self):
        out = serialize_fields(FIELDS)
        self.assertIn('[0] text "Vorname *"  —  required', out)
        self.assertIn("options: Bitte wählen | Herr | Frau", out)
        self.assertIn("accepts .pdf,.docx", out)

    def test_label_uncertain_and_custom_flags(self):
        lines = serialize_fields(FIELDS).splitlines()
        self.assertIn("label-uncertain", lines[4])
        self.assertIn("custom-widget", lines[3])

    def test_empty_label_falls_back_to_name(self):
        lines = serialize_fields(FIELDS).splitlines()
        self.assertIn('"field_x"', lines[5])


class GroundTest(unittest.TestCase):
    def test_valid_index_returns_field(self):
        self.assertEqual(ground(FIELDS, 0)["selector"], "#fn")
        self.assertEqual(ground(FIELDS, len(FIELDS) - 1)["selector"], "#noname")

    def test_rejects_bogus_ids(self):
        for bad in (-1, len(FIELDS), "2", True, None):
            self.assertIsNone(ground(FIELDS, bad))


if __name__ == "__main__":
    unittest.main()
