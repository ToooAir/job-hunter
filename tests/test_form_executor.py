"""Integration tests for utils/form_executor.py against local fixture pages.

Mirrors tests/test_browser.py: needs Playwright (present in the pipeline
container); the whole module skips cleanly on hosts without it.

Run:  python -m unittest tests.test_form_executor -v
"""

import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import playwright  # noqa: F401
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

import utils.form_executor as fx  # noqa: E402
from utils.form_executor import (  # noqa: E402
    execute_action,
    execute_actions,
    resolve_upload_path,
)

if HAS_PLAYWRIGHT:
    from utils.browser import headless_session

# Fictional Max Mustermann data only (committed fixture policy).
# Radio input values (mr/ms) deliberately differ from their labels
# (Herr/Frau): the payload carries labels — the executor must resolve
# by name group + label text, never by value attribute alone.
FORM_HTML = """<!doctype html><html><body>
<form>
  <label for="fn">Vorname</label><input id="fn" name="first_name">
  <label for="em">E-Mail</label><input id="em" type="email" name="email">
  <label for="msg">Anschreiben</label><textarea id="msg" name="message"></textarea>
  <label for="country">Land</label>
  <select id="country" name="country">
    <option value="">Bitte wählen</option>
    <option value="de">Deutschland</option>
    <option value="at">Österreich</option>
  </select>
  <label><input type="checkbox" id="privacy" name="privacy"> Datenschutz gelesen</label>
  <fieldset>
    <legend>Anrede</legend>
    <label><input type="radio" name="salutation" value="mr"> Herr</label>
    <label><input type="radio" name="salutation" value="ms"> Frau</label>
  </fieldset>
  <input type="file" id="cv" name="cv">
</form>
</body></html>"""

IFRAME_HTML = """<!doctype html><html><body>
<h1>Karriere</h1>
<iframe id="apply-frame" src="form.html" style="width:700px;height:500px"></iframe>
</body></html>"""


def action(selector, act, value="", kind="text", **kw):
    return {"selector": selector, "kind": kind, "label": kw.pop("label", ""),
            "action": act, "value": value, "source": "profile:test",
            "needs_review": False, **kw}


class UploadPathTest(unittest.TestCase):
    """Pure logic — runs everywhere, no browser needed."""

    def test_relative_path_resolves_against_project_root(self):
        p = resolve_upload_path("candidate_kb/cv/cv.pdf")
        self.assertEqual(p, fx.ROOT / "candidate_kb" / "cv" / "cv.pdf")

    def test_absolute_path_kept(self):
        self.assertEqual(resolve_upload_path("/tmp/x.pdf"), Path("/tmp/x.pdf"))


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed on this host")
class ExecutorTest(unittest.TestCase):
    """One shared headless session + fixture HTTP server for all tests."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "site").mkdir()
        (root / "site" / "form.html").write_text(FORM_HTML, encoding="utf-8")
        (root / "site" / "iframe.html").write_text(IFRAME_HTML, encoding="utf-8")

        handler = partial(SimpleHTTPRequestHandler, directory=str(root / "site"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

        cls.session_cm = headless_session(profile_dir=root / "profile")
        cls.context = cls.session_cm.__enter__()
        cls._timeout = fx.ACTION_TIMEOUT_MS
        fx.ACTION_TIMEOUT_MS = 800  # fail fast on the deliberate-error tests

    @classmethod
    def tearDownClass(cls):
        fx.ACTION_TIMEOUT_MS = cls._timeout
        cls.session_cm.__exit__(None, None, None)
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.page = self.context.new_page()
        self.page.goto(f"{self.base}/form.html")

    def tearDown(self):
        self.page.close()

    def test_full_payload_roundtrip(self):
        out = execute_actions(self.page, [
            action("#fn", "fill", "Max"),
            action("#em", "fill", "max.mustermann@example.com", kind="email"),
            action("#msg", "fill", "Dear team, ...", kind="textarea"),
            action("#country", "select_option", "Deutschland", kind="select"),
            action("#privacy", "check", kind="checkbox"),
        ])
        self.assertEqual(out["failed"], 0, out["results"])
        self.assertEqual(self.page.input_value("#fn"), "Max")
        self.assertEqual(self.page.input_value("#em"), "max.mustermann@example.com")
        self.assertEqual(self.page.input_value("#msg"), "Dear team, ...")
        self.assertEqual(self.page.input_value("#country"), "de")
        self.assertTrue(self.page.is_checked("#privacy"))

    def test_radio_resolved_by_label_not_value_attribute(self):
        # payload selector points at the FIRST radio of the group ('Herr'),
        # but the approved value is 'Frau' — the executor must check value=ms
        res = execute_action(self.page, action(
            'input[name="salutation"][value="mr"]', "select_option",
            "Frau", kind="radio"))
        self.assertTrue(res["ok"], res["error"])
        self.assertTrue(self.page.is_checked('input[value="ms"]'))
        self.assertFalse(self.page.is_checked('input[value="mr"]'))

    def test_radio_unknown_option_reports_error(self):
        res = execute_action(self.page, action(
            'input[name="salutation"][value="mr"]', "select_option",
            "Divers", kind="radio"))
        self.assertFalse(res["ok"])
        self.assertIn("radio-option-not-found", res["error"])

    def test_select_falls_back_to_value_attribute(self):
        res = execute_action(self.page, action(
            "#country", "select_option", "at", kind="select"))
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(self.page.input_value("#country"), "at")

    def test_broken_selector_does_not_abort_batch(self):
        out = execute_actions(self.page, [
            action("#does-not-exist", "fill", "x"),
            action("#fn", "fill", "Max"),
        ])
        self.assertEqual(out["failed"], 1)
        self.assertFalse(out["results"][0]["ok"])
        self.assertIsNotNone(out["results"][0]["error"])
        self.assertTrue(out["results"][1]["ok"])  # batch continued
        self.assertEqual(self.page.input_value("#fn"), "Max")

    def test_skip_action_is_a_successful_noop(self):
        res = execute_action(self.page, action("#fn", "skip"))
        self.assertTrue(res["ok"])
        self.assertEqual(self.page.input_value("#fn"), "")

    def test_unknown_action_reports_error(self):
        res = execute_action(self.page, action("#fn", "teleport", "x"))
        self.assertFalse(res["ok"])
        self.assertIn("unknown-action", res["error"])

    def test_upload_resolves_relative_path_and_sets_file(self):
        # requirements.txt exists at the project root in every environment
        res = execute_action(self.page, action(
            "#cv", "upload", "requirements.txt", kind="file"))
        self.assertTrue(res["ok"], res["error"])
        name = self.page.evaluate(
            "document.querySelector('#cv').files[0].name")
        self.assertEqual(name, "requirements.txt")

    def test_upload_missing_file_reports_error(self):
        res = execute_action(self.page, action(
            "#cv", "upload", "candidate_kb/cv/missing.pdf", kind="file"))
        self.assertFalse(res["ok"])
        self.assertIn("file-not-found", res["error"])

    def test_frame_path_descends_into_iframe(self):
        self.page.goto(f"{self.base}/iframe.html")
        out = execute_actions(self.page, [
            action("#fn", "fill", "Max", frame_path=["iframe#apply-frame"]),
            action('input[name="salutation"][value="mr"]', "select_option",
                   "Frau", kind="radio", frame_path=["iframe#apply-frame"]),
        ])
        self.assertEqual(out["failed"], 0, out["results"])
        frame = self.page.frame_locator("iframe#apply-frame")
        self.assertEqual(frame.locator("#fn").input_value(), "Max")
        self.assertTrue(frame.locator('input[value="ms"]').is_checked())


if __name__ == "__main__":
    unittest.main()
