"""Tests for utils/profile_loader.py.

Integration tests run against the committed candidate_profile.yaml.example,
so a template edit that breaks the loader (or vice versa) fails CI here.

Run:  python -m unittest discover tests -v
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.profile_loader import (  # noqa: E402
    CandidateProfile,
    ProfileError,
    ProfileIncompleteError,
    load_profile,
)

EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "candidate_kb" / "candidate_profile.yaml.example"

COMPLETE_YAML = """\
meta:
  cl_language: en
  cv_path: "{cv_path}"

fields:
  first_name:
    value: "Max"
    aliases: [first name, given name, vorname]
  full_name:
    value: "Max Mustermann"
    aliases: [full name, name]
  country:
    value: "Germany"
    aliases: [country, land, staat]
  earliest_start:
    value: "Immediately"
    date_value: "+14 days"
    aliases: [earliest start, eintrittstermin]
  salary_expectation:
    value: "€65,000 gross per year"
    value_eur_year: 65000
    aliases: [salary expectation, gehaltsvorstellung]

consents:
  auto_accept_aliases: [datenschutz, datenschutzerklärung]

never_fill:
  - date of birth / geburtsdatum
  - marital status / familienstand
"""


class ExampleTemplateTest(unittest.TestCase):
    """The committed template must stay loadable and its TODOs detectable."""

    def setUp(self):
        self.profile = load_profile(EXAMPLE_PATH, strict=False)

    def test_strict_load_refuses_template(self):
        with self.assertRaises(ProfileIncompleteError):
            load_profile(EXAMPLE_PATH, strict=True, check_cv_file=False)

    def test_todo_residue_lists_user_homework(self):
        todos = set(self.profile.todo_residue(check_cv_file=False))
        expected = {
            "fields.first_name.value",
            "fields.last_name.value",
            "fields.full_name.value",
            "fields.email.value",
            "fields.phone.value",
            "fields.street_address.value",
            "fields.postal_code.value",
            "fields.city.value",
            "fields.current_location.value",
            "fields.nationality.value",
            "fields.languages.value",
            "fields.german_level.value",
            "fields.salary_expectation.value",
            "fields.salary_expectation.value_eur_year",
            "fields.linkedin.value",
            "fields.github.value",
            "fields.date_of_birth.value",
            "fields.gender.value",
            "fields.disability_status.value",
        }
        self.assertEqual(todos, expected)

    def test_german_label_matching(self):
        cases = {
            "Vorname *": "first_name",
            "Nachname": "last_name",
            "E-Mail-Adresse": "email",
            "Telefonnummer": "phone",
            "PLZ": "postal_code",
            "Gehaltsvorstellung (brutto / Jahr)": "salary_expectation",
            "Frühestmöglicher Eintrittstermin": "earliest_start",
            "Kündigungsfrist": "notice_period",
            "Staatsangehörigkeit": "nationality",
        }
        for label, expected_key in cases.items():
            with self.subTest(label=label):
                match = self.profile.match_field(label)
                self.assertIsNotNone(match, f"no match for {label!r}")
                self.assertEqual(match.key, expected_key)

    def test_english_label_matching(self):
        self.assertEqual(self.profile.match_field("First Name").key, "first_name")
        self.assertEqual(self.profile.match_field("Expected salary").key, "salary_expectation")
        self.assertEqual(self.profile.match_field("Right to work in Germany?").key, "work_permit")

    def test_longest_alias_wins(self):
        # "first name" (first_name) must beat the generic "name" (full_name)
        self.assertEqual(self.profile.match_field("first name").key, "first_name")
        self.assertEqual(self.profile.match_field("Name").key, "full_name")

    def test_no_substring_false_positive(self):
        # "land" (country) must not match inside the word "Deutschland"
        self.assertIsNone(self.profile.match_field("Deutschland"))

    def test_unmatched_label_returns_none(self):
        self.assertIsNone(self.profile.match_field("Why do you want to work here?"))

    def test_never_fill(self):
        self.assertTrue(self.profile.is_never_fill("Bewerbungsfoto"))
        self.assertTrue(self.profile.is_never_fill("Marital status"))
        # Geburtsdatum was promoted to an optional field (common on German forms)
        self.assertFalse(self.profile.is_never_fill("Geburtsdatum *"))
        self.assertFalse(self.profile.is_never_fill("First name"))

    def test_optional_disclosure_fields_match(self):
        self.assertEqual(self.profile.match_field("Geburtsdatum").key, "date_of_birth")
        self.assertEqual(self.profile.match_field("Geschlecht").key, "gender")
        self.assertEqual(self.profile.match_field("Schwerbehinderung").key, "disability_status")

    def test_auto_consent(self):
        self.assertTrue(self.profile.is_auto_consent("Ich akzeptiere die Datenschutzerklärung"))
        self.assertFalse(self.profile.is_auto_consent("Subscribe to newsletter"))


class CompleteProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cv = Path(self.tmp.name) / "cv.pdf"
        self.cv.write_bytes(b"%PDF-1.4 fake")
        self.path = Path(self.tmp.name) / "profile.yaml"
        self.path.write_text(COMPLETE_YAML.format(cv_path=self.cv), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_strict_load_succeeds(self):
        profile = load_profile(self.path, strict=True)
        self.assertEqual(profile.cl_language, "en")
        self.assertEqual(profile.cv_path, self.cv)

    def test_missing_cv_file_fails_strict(self):
        self.cv.unlink()
        with self.assertRaises(ProfileIncompleteError):
            load_profile(self.path, strict=True)
        # but loads when the CV check is waived (e.g. content-only stages)
        load_profile(self.path, strict=True, check_cv_file=False)

    def test_date_resolution(self):
        profile = load_profile(self.path, strict=True)
        start = profile.fields["earliest_start"]
        self.assertEqual(start.resolve_date(today=date(2026, 6, 11)), "2026-06-25")
        self.assertIsNone(profile.fields["first_name"].resolve_date())

    def test_concrete_date_spec_passes_through(self):
        field = CandidateProfile(
            {"fields": {"x": {"value": "v", "date_value": "2026-08-01", "aliases": ["x"]}}}
        ).fields["x"]
        self.assertEqual(field.resolve_date(today=date(2026, 6, 11)), "2026-08-01")


class MalformedProfileTest(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(ProfileError):
            load_profile("/nonexistent/profile.yaml")

    def test_yaml_without_fields_section(self):
        with self.assertRaises(ProfileError):
            CandidateProfile({"meta": {}})

    def test_field_without_value(self):
        with self.assertRaises(ProfileError):
            CandidateProfile({"fields": {"x": {"aliases": ["x"]}}})

    def test_invalid_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.yaml"
            p.write_text("fields: [unclosed", encoding="utf-8")
            with self.assertRaises(ProfileError):
                load_profile(p)


if __name__ == "__main__":
    unittest.main()
