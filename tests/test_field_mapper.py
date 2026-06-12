"""Tests for utils/field_mapper.py (Step 4.1 deterministic mapping).

Field fixtures mirror real shapes seen in the Step 3 browser probe
(jobware-style template, Personio digit-id selectors, consent checkboxes);
all personal values are fictional Max Mustermann data.
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.field_mapper import ground_option, map_fields  # noqa: E402
from utils.profile_loader import CandidateProfile  # noqa: E402

PROFILE_DATA = {
    "meta": {"cl_language": "en", "cv_path": "candidate_kb/cv/cv.pdf"},
    "fields": {
        "salutation": {"value": "Herr", "aliases": ["salutation", "anrede"]},
        "first_name": {"value": "Max", "aliases": ["first name", "given name", "vorname"]},
        "last_name": {"value": "Mustermann", "aliases": ["last name", "surname", "nachname"]},
        "full_name": {"value": "Max Mustermann", "aliases": ["full name", "name"]},
        "email": {"value": "max.mustermann@example.com", "aliases": ["email", "e-mail"]},
        "phone": {"value": "+49 151 2345678", "aliases": ["phone", "telefon", "telefonnummer"]},
        "city": {"value": "Hamburg", "aliases": ["city", "stadt", "ort"]},
        "country": {"value": "Germany", "aliases": ["country", "land"]},
        "gender": {"value": "Männlich", "aliases": ["gender", "geschlecht"]},
        "date_of_birth": {"value": "01.04.1995", "aliases": ["date of birth", "geburtsdatum"]},
        "earliest_start": {
            "value": "Immediately", "date_value": "+14 days",
            "aliases": ["earliest start", "eintrittstermin", "verfügbar ab"],
        },
        "salary_expectation": {
            "value": "€70,000 gross per year (negotiable)", "value_eur_year": 70000,
            "aliases": ["salary expectation", "gehaltsvorstellung", "gehalt"],
        },
        "languages": {"value": "English (fluent), German (A2)",
                      "aliases": ["languages", "sprachkenntnisse"]},
    },
    "consents": {"auto_accept_aliases": ["datenschutz", "privacy policy",
                                         "datenschutzerklärung"]},
    "never_fill": ["photo / bewerbungsfoto", "religion / konfession"],
}

PROFILE = CandidateProfile(PROFILE_DATA)
TODAY = date(2026, 6, 12)


def run(fields, **kw):
    return map_fields(fields, PROFILE, today=TODAY, **kw)


def f(**kw):
    base = {"selector": '[name="x"]', "kind": "text", "label": ""}
    base.update(kw)
    return base


class TestTextMapping(unittest.TestCase):
    def test_basic_german_labels_map(self):
        out = run([
            f(selector="#vn", label="Vorname *", name="first_name"),
            f(selector="#nn", label="Nachname *", name="last_name"),
            f(selector="#em", label="E-Mail-Adresse *", kind="email", name="email"),
        ])
        values = {a["selector"]: a["value"] for a in out["actions"]}
        self.assertEqual(values, {"#vn": "Max", "#nn": "Mustermann",
                                  "#em": "max.mustermann@example.com"})
        self.assertFalse(out["pending"])

    def test_source_records_profile_key(self):
        out = run([f(label="Vorname", name="first_name")])
        self.assertEqual(out["actions"][0]["source"], "profile:first_name")
        self.assertFalse(out["actions"][0]["needs_review"])

    def test_autocomplete_beats_useless_label(self):
        out = run([f(label="Feld 3", autocomplete="given-name")])
        self.assertEqual(out["actions"][0]["value"], "Max")

    def test_kind_fallback_for_unlabeled_email_and_tel(self):
        out = run([f(kind="email", label="Bitte angeben"),
                   f(kind="tel", label="Bitte angeben")])
        self.assertEqual([a["value"] for a in out["actions"]],
                         ["max.mustermann@example.com", "+49 151 2345678"])

    def test_name_attr_matching_handles_brackets(self):
        out = run([f(label="", name="application[first_name]")])
        self.assertEqual(out["actions"][0]["value"], "Max")

    def test_company_name_field_is_not_the_candidate(self):
        out = run([f(label="Name der Firma")])
        self.assertEqual(out["pending"][0]["reason"], "no-deterministic-match")

    def test_unmatched_required_field_stays_pending_with_required(self):
        out = run([f(label="Referenznummer", required=True)])
        self.assertTrue(out["pending"][0]["required"])

    def test_frame_path_carried_into_action(self):
        out = run([f(label="Vorname", frame_path=["iframe#apply-frame"])])
        self.assertEqual(out["actions"][0]["frame_path"], ["iframe#apply-frame"])


class TestChoiceFields(unittest.TestCase):
    def test_select_grounds_exact_option(self):
        out = run([f(kind="select", label="Anrede", options=["Bitte wählen", "Herr", "Frau"])])
        a = out["actions"][0]
        self.assertEqual((a["action"], a["value"]), ("select_option", "Herr"))

    def test_country_grounds_via_synonym(self):
        out = run([f(kind="select", label="Land",
                     options=["Deutschland", "Österreich", "Schweiz"])])
        self.assertEqual(out["actions"][0]["value"], "Deutschland")

    def test_radio_gender_grounds(self):
        out = run([f(kind="radio", label="Geschlecht", name="gender",
                     options=["Männlich", "Weiblich", "Divers"])])
        self.assertEqual(out["actions"][0]["value"], "Männlich")

    def test_ungrounded_option_goes_pending_with_suggestion(self):
        out = run([f(kind="select", label="Gehaltsvorstellung",
                     options=["bis 50.000", "50.000-60.000", "über 60.000"])])
        p = out["pending"][0]
        self.assertEqual(p["reason"], "option-not-grounded")
        self.assertEqual(p["suggestion_source"], "profile:salary_expectation")

    def test_ground_option_rejects_tiny_substrings(self):
        self.assertIsNone(ground_option("no", ["Nordrhein-Westfalen kennen"]))
        self.assertEqual(ground_option("no", ["Ja", "Nein"]), "Nein")


class TestCheckboxes(unittest.TestCase):
    def test_privacy_consent_auto_checked(self):
        out = run([f(kind="checkbox",
                     label="Ich habe die Datenschutzerklärung gelesen *")])
        self.assertEqual(out["actions"][0]["action"], "check")
        self.assertEqual(out["actions"][0]["source"], "profile:consent")

    def test_unknown_checkbox_pending(self):
        out = run([f(kind="checkbox", label="Newsletter abonnieren")])
        self.assertEqual(out["pending"][0]["reason"], "checkbox-unknown")


class TestDatesAndNumbers(unittest.TestCase):
    def test_date_spec_resolves_relative_date(self):
        out = run([f(kind="date", label="Frühestmöglicher Eintrittstermin")])
        self.assertEqual(out["actions"][0]["value"], "2026-06-26")  # +14 days

    def test_german_birthdate_converted_to_iso_for_date_input(self):
        out = run([f(kind="date", label="Geburtsdatum")])
        self.assertEqual(out["actions"][0]["value"], "1995-04-01")

    def test_text_birthdate_keeps_profile_format(self):
        out = run([f(kind="text", label="Geburtsdatum")])
        self.assertEqual(out["actions"][0]["value"], "01.04.1995")

    def test_salary_number_input_uses_bare_number(self):
        out = run([f(kind="number", label="Gehaltsvorstellung (EUR/Jahr)")])
        self.assertEqual(out["actions"][0]["value"], "70000")

    def test_salary_text_input_uses_full_phrase(self):
        out = run([f(kind="text", label="Gehaltsvorstellung")])
        self.assertIn("negotiable", out["actions"][0]["value"])


class TestFilesAndContent(unittest.TestCase):
    def test_cv_upload_mapped_to_profile_path(self):
        out = run([f(kind="file", label="Lebenslauf *", accept=".pdf,.doc")])
        a = out["actions"][0]
        self.assertEqual((a["action"], a["value"], a["source"]),
                         ("upload", "candidate_kb/cv/cv.pdf", "profile:cv"))

    def test_cover_letter_upload_surfaces_as_unfilled(self):
        out = run([f(kind="file", label="Anschreiben")])
        self.assertEqual(out["unfilled"][0]["reason"], "cover-letter-upload")

    def test_other_attachment_slot_never_silently_dropped(self):
        out = run([f(kind="file", label="Zeugnisse", required=True)])
        u = out["unfilled"][0]
        self.assertEqual(u["reason"], "attachment-unmapped")
        self.assertTrue(u["required"])

    def test_cover_letter_textarea_routed_to_content_node(self):
        out = run([f(kind="textarea", label="Anschreiben / Motivation")])
        self.assertEqual(out["pending"][0]["reason"], "cover-letter-slot")

    def test_custom_widget_suggests_but_does_not_act(self):
        out = run([f(kind="custom", label="Land", options=[])])
        p = out["pending"][0]
        self.assertEqual(p["reason"], "custom-widget")
        self.assertEqual(p["suggestion"], "Germany")


class TestNeverFillAndAccounting(unittest.TestCase):
    def test_never_fill_label_skipped(self):
        out = run([f(kind="file", label="Bewerbungsfoto")])
        self.assertEqual(out["never_fill_skipped"], ["Bewerbungsfoto"])
        self.assertFalse(out["actions"] + out["unfilled"])

    def test_every_field_lands_in_exactly_one_bucket(self):
        fields = [
            f(label="Vorname"), f(label="Referenznummer"),
            f(kind="file", label="Lebenslauf"), f(kind="file", label="Zeugnisse"),
            f(kind="checkbox", label="Datenschutz akzeptieren"),
            f(label="Konfession"),
        ]
        out = run(fields)
        total = (len(out["actions"]) + len(out["pending"])
                 + len(out["unfilled"]) + len(out["never_fill_skipped"]))
        self.assertEqual(total, len(fields))

    def test_languages_textarea_maps_deterministically(self):
        out = run([f(kind="textarea", label="Sprachkenntnisse")])
        self.assertIn("German (A2)", out["actions"][0]["value"])


if __name__ == "__main__":
    unittest.main()
