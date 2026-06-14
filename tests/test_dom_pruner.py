"""Tests for utils/dom_pruner.py (offline, fixtures use fictional data only).

Run:  python -m unittest tests.test_dom_pruner -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.dom_pruner import extract_fields, prune_html  # noqa: E402

GERMAN_FORM = """\
<html><head><script>tracking();</script><style>.x{color:red}</style></head>
<body>
<nav><a href="/">Start</a><a href="/jobs">Jobs</a></nav>
<header><h1>Mustermann GmbH Karriere</h1></header>
<!-- application form below -->
<main>
<p>Werden Sie Teil unseres Teams in Beispielstadt.</p>
<form action="/apply" method="post" class="apply-form" data-track="x">
  <input type="hidden" name="csrf" value="token123">
  <label for="vorname">Vorname *</label>
  <input type="text" id="vorname" name="first_name" required>
  <label for="nachname">Nachname *</label>
  <input type="text" id="nachname" name="last_name" required>
  <label for="email">E-Mail-Adresse</label>
  <input type="email" id="email" name="email" autocomplete="email">
  <input type="tel" name="phone" placeholder="Telefonnummer">
  <div>
    <label for="gehalt">Gehaltsvorstellung (brutto / Jahr)</label>
    <input type="number" id="gehalt" name="salary">
  </div>
  <fieldset>
    <legend>Anrede</legend>
    <label><input type="radio" name="anrede" value="herr"> Herr</label>
    <label><input type="radio" name="anrede" value="frau"> Frau</label>
    <label><input type="radio" name="anrede" value="divers"> Divers</label>
  </fieldset>
  <label for="land">Land</label>
  <select id="land" name="country">
    <option value="">Bitte wählen</option>
    <option value="de">Deutschland</option>
    <option value="at">Österreich</option>
  </select>
  <label for="cl">Anschreiben</label>
  <textarea id="cl" name="cover_letter"></textarea>
  <label for="cv">Lebenslauf hochladen *</label>
  <input type="file" id="cv" name="resume" accept=".pdf,.docx" required>
  <label><input type="checkbox" name="privacy" required> Ich akzeptiere die Datenschutzerklärung</label>
  <input type="submit" value="Bewerbung absenden">
</form>
</main>
<footer><p>© Mustermann GmbH</p></footer>
</body></html>
"""

NO_FORM_TAG_SPA = """\
<html><body>
<div id="app">
  <div class="application">
    <span>Vollständiger Name</span>
    <input type="text" name="full_name">
    <div role="combobox" aria-label="Standort" id="loc-picker">Standort wählen</div>
    <textarea aria-label="Warum möchten Sie bei uns arbeiten?" name="motivation"></textarea>
  </div>
</div>
</body></html>
"""


class ExtractFieldsTest(unittest.TestCase):
    def setUp(self):
        self.fields = extract_fields(GERMAN_FORM)
        self.by_name = {f.name: f for f in self.fields}

    def test_hidden_and_submit_skipped(self):
        self.assertNotIn("csrf", self.by_name)
        self.assertFalse(any(f.kind in ("hidden", "submit") for f in self.fields))

    def test_label_via_for_attribute(self):
        self.assertEqual(self.by_name["first_name"].label, "Vorname *")
        self.assertEqual(self.by_name["salary"].label, "Gehaltsvorstellung (brutto / Jahr)")

    def test_placeholder_fallback_label(self):
        self.assertEqual(self.by_name["phone"].label, "Telefonnummer")
        self.assertEqual(self.by_name["phone"].kind, "tel")

    def test_required_detection(self):
        self.assertTrue(self.by_name["first_name"].required)   # required attr
        self.assertFalse(self.by_name["email"].required)
        self.assertTrue(self.by_name["resume"].required)

    def test_radio_group_collapsed_with_legend_label(self):
        anrede = self.by_name["anrede"]
        self.assertEqual(anrede.kind, "radio")
        self.assertEqual(anrede.label, "Anrede")
        self.assertEqual(anrede.options, ["Herr", "Frau", "Divers"])
        # only ONE field for the whole group
        self.assertEqual(sum(1 for f in self.fields if f.name == "anrede"), 1)

    def test_select_options_extracted(self):
        country = self.by_name["country"]
        self.assertEqual(country.kind, "select")
        self.assertIn("Deutschland", country.options)

    def test_file_input_marked_with_accept(self):
        cv = self.by_name["resume"]
        self.assertEqual(cv.kind, "file")
        self.assertEqual(cv.accept, ".pdf,.docx")
        self.assertEqual(cv.label, "Lebenslauf hochladen *")

    def test_checkbox_with_wrapping_label(self):
        privacy = self.by_name["privacy"]
        self.assertEqual(privacy.kind, "checkbox")
        self.assertIn("Datenschutzerklärung", privacy.label)

    def test_selectors_unique_and_resolvable(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(GERMAN_FORM, "html.parser")
        for f in self.fields:
            with self.subTest(field=f.name or f.label):
                self.assertEqual(len(soup.select(f.selector)), 1, f.selector)

    def test_id_preferred_in_selector(self):
        self.assertEqual(self.by_name["email"].selector, "#email")

    def test_frame_path_passthrough(self):
        fields = extract_fields(GERMAN_FORM, frame_path=("iframe#apply",))
        self.assertEqual(fields[0].frame_path, ("iframe#apply",))
        self.assertEqual(fields[0].to_dict()["frame_path"], ["iframe#apply"])

    def test_autocomplete_kept(self):
        self.assertEqual(self.by_name["email"].autocomplete, "email")


class SpaWithoutFormTagTest(unittest.TestCase):
    def setUp(self):
        self.fields = extract_fields(NO_FORM_TAG_SPA)

    def test_custom_widget_detected(self):
        custom = [f for f in self.fields if f.kind == "custom"]
        self.assertEqual(len(custom), 1)
        # aria-label takes priority over inner text for custom widgets
        self.assertEqual(custom[0].label, "Standort")

    def test_sibling_text_label(self):
        name = next(f for f in self.fields if f.name == "full_name")
        self.assertEqual(name.label, "Vollständiger Name")

    def test_aria_label_textarea(self):
        motivation = next(f for f in self.fields if f.name == "motivation")
        self.assertEqual(motivation.label, "Warum möchten Sie bei uns arbeiten?")


# Board page shaped like the Step 3 probe's jobware false positive: a site
# search in the header, cookie-preference checkboxes, and the actual apply
# controls in SPA markup (no <form> tag).
BOARD_CHROME_PAGE = """\
<html><body>
<header>
  <form action="/suche" id="job-search">
    <input type="text" name="keyword" placeholder="Stichwort, Jobtitel oder Firma">
    <input type="text" name="location" placeholder="PLZ, Ort oder Land">
  </form>
</header>
<div class="usercentrics-root">
  <label><input type="checkbox" name="consent_stats"> Statistik</label>
  <label><input type="checkbox" name="consent_marketing"> Marketing</label>
</div>
<main>
  <div id="apply-widget">
    <span>Vorname</span>
    <input type="text" name="first_name">
    <span>E-Mail-Adresse</span>
    <input type="email" name="applicant_email">
  </div>
</main>
</body></html>
"""

# Login form next to the real application form (Step 3 probe saw exactly
# this on a wearedevelopers company site).
LOGIN_PLUS_APPLY = """\
<html><body>
<form action="/login" id="login">
  <label>E-Mail:</label><input type="email" name="login_email">
  <label>Passwort:</label><input type="password" name="password">
</form>
<form action="/bewerbung" id="apply">
  <label for="afn">Vorname</label><input id="afn" type="text" name="first_name">
  <label for="aln">Nachname</label><input id="aln" type="text" name="last_name">
  <label for="acv">Lebenslauf</label><input id="acv" type="file" name="cv">
</form>
</body></html>
"""


class FormScopingTest(unittest.TestCase):
    def test_search_and_cookie_chrome_filtered_on_formless_page(self):
        names = {f.name for f in extract_fields(BOARD_CHROME_PAGE)}
        self.assertEqual(names, {"first_name", "applicant_email"})

    def test_login_form_never_chosen_as_application_form(self):
        names = {f.name for f in extract_fields(LOGIN_PLUS_APPLY)}
        self.assertEqual(names, {"first_name", "last_name", "cv"})

    def test_password_inputs_always_skipped(self):
        fields = extract_fields(LOGIN_PLUS_APPLY, scope_to_form=False)
        self.assertFalse(any(f.name == "password" for f in fields))

    def test_scoping_can_be_disabled(self):
        names = {f.name for f in extract_fields(LOGIN_PLUS_APPLY, scope_to_form=False)}
        self.assertIn("login_email", names)

    def test_real_form_page_unchanged_by_scoping(self):
        names = {f.name for f in extract_fields(GERMAN_FORM)}
        self.assertEqual(names, {"first_name", "last_name", "email", "phone",
                                 "salary", "anrede", "country", "cover_letter",
                                 "resume", "privacy"})


# Misaligned markup (milia.io shape, watchlist #4): a control with no label of
# its own, preceded by another field's label / an intervening input.
LABEL_MISALIGN = """\
<form action="/apply">
  <input type="file" name="cv">
  <span>Portfolio URL</span>
  <input type="url" name="portfolio">
  <label for="years">Years of Experience</label>
  <input type="url" name="github">
  <input type="number" name="years" id="years">
</form>
"""


class LabelCrossValidationTest(unittest.TestCase):
    def setUp(self):
        self.by_name = {f.name: f for f in extract_fields(LABEL_MISALIGN)}

    def test_sibling_text_label_is_used_but_flagged(self):
        portfolio = self.by_name["portfolio"]
        self.assertEqual(portfolio.label, "Portfolio URL")
        self.assertTrue(portfolio.label_suspect)        # positional guess
        self.assertTrue(portfolio.to_dict()["label_suspect"])

    def test_does_not_borrow_other_fields_label(self):
        # github sits after <label for="years"> — must NOT inherit it.
        github = self.by_name["github"]
        self.assertNotEqual(github.label, "Years of Experience")
        self.assertEqual(github.label, "github")        # name fallback
        self.assertFalse(github.label_suspect)

    def test_explicit_for_label_is_trusted(self):
        years = self.by_name["years"]
        self.assertEqual(years.label, "Years of Experience")
        self.assertFalse(years.label_suspect)


class PruneHtmlTest(unittest.TestCase):
    def test_scripts_styles_chrome_comments_removed(self):
        out = prune_html(GERMAN_FORM)
        for forbidden in ("tracking()", "color:red", "<nav", "<footer", "application form below"):
            self.assertNotIn(forbidden, out)

    def test_form_and_semantics_kept(self):
        out = prune_html(GERMAN_FORM)
        for required in ("Vorname", "Gehaltsvorstellung", 'name="resume"',
                         "Datenschutzerklärung", 'autocomplete="email"'):
            self.assertIn(required, out)

    def test_non_semantic_attributes_dropped(self):
        out = prune_html(GERMAN_FORM)
        self.assertNotIn("data-track", out)
        self.assertNotIn("apply-form", out)  # class attr dropped

    def test_no_form_tag_fallback(self):
        out = prune_html(NO_FORM_TAG_SPA)
        self.assertIn('name="full_name"', out)
        self.assertIn("combobox", out)

    def test_budget_enforced_on_bloated_page(self):
        filler = "".join(
            f"<p>Absatz {i} über unsere Unternehmenskultur und Geschichte.</p>" for i in range(2000)
        )
        bloated = GERMAN_FORM.replace("<form", filler + "<form")
        out = prune_html(bloated, budget=20_000)
        # form survives; the dropped filler was outside the form root anyway
        self.assertIn('name="first_name"', out)
        self.assertLess(len(out.encode()), 25_000)


# Lever posts each custom-question card as JSON in a hidden baseTemplate input;
# the answer controls are cards[UUID][fieldN] whose only DOM label is that
# opaque name. Fictional questions only.
_CARD_JSON = json.dumps({"fields": [
    {"type": "textarea", "text": "What is your favourite programming language?"},
    {"type": "textarea", "text": "Why do you want to join Mustermann GmbH?"},
]})
LEVER_FORM = f"""\
<html><body><form>
  <input type="text" name="name" />
  <input type="hidden" name="cards[abc-123][baseTemplate]" value='{_CARD_JSON}' />
  <textarea name="cards[abc-123][field0]"></textarea>
  <textarea name="cards[abc-123][field1]"></textarea>
</form></body></html>"""


class LeverCardQuestionTest(unittest.TestCase):
    def test_opaque_card_fields_get_real_question(self):
        by_name = {f.name: f for f in extract_fields(LEVER_FORM)}
        self.assertEqual(by_name["cards[abc-123][field0]"].context_hint,
                         "What is your favourite programming language?")
        self.assertEqual(by_name["cards[abc-123][field1]"].context_hint,
                         "Why do you want to join Mustermann GmbH?")

    def test_context_hint_in_to_dict_only_when_present(self):
        by_name = {f.name: f for f in extract_fields(LEVER_FORM)}
        self.assertNotIn("context_hint", by_name["name"].to_dict())
        self.assertIn("context_hint", by_name["cards[abc-123][field0]"].to_dict())

    def test_malformed_card_json_is_ignored(self):
        bad = LEVER_FORM.replace(_CARD_JSON, "{not json")
        by_name = {f.name: f for f in extract_fields(bad)}  # must not raise
        self.assertEqual(by_name["cards[abc-123][field0]"].context_hint, "")


if __name__ == "__main__":
    unittest.main()
