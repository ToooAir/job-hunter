"""Integration tests for utils/browser.py against local fixture pages.

Needs Playwright (present in the pipeline container); the whole module
skips cleanly on hosts without it.

Run:  python -m unittest tests.test_browser -v
"""

import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.playwright_guard import has_chromium

HAS_PLAYWRIGHT = has_chromium()  # package alone is not enough on the host

if HAS_PLAYWRIGHT:
    from utils.browser import (
        ProfileBusyError,
        extract_form_tree,
        goto_apply_page,
        headless_session,
        profile_lock,
    )

COOKIE_BANNER = """
<div id="consent" style="position:fixed;inset:0;background:#000c;z-index:99">
  <button onclick="document.title='accepted'">Alle akzeptieren</button>
  <button onclick="document.getElementById('consent').remove();document.title='declined'">
    Nur notwendige Cookies akzeptieren
  </button>
</div>
"""

DETAIL_HTML = f"""<!doctype html><html><head><title>job</title></head><body>
{COOKIE_BANNER}
<h1>Senior Engineer (m/w/d) — Mustermann GmbH</h1>
<p>Wir suchen Verstärkung in Beispielstadt.</p>
<a href="form.html">Jetzt bewerben</a>
</body></html>"""

FORM_HTML = """<!doctype html><html><body>
<form>
  <label for="vn">Vorname</label><input id="vn" name="first_name">
  <label for="em">E-Mail</label><input id="em" type="email" name="email">
  <input type="file" name="cv">
</form>
</body></html>"""

IFRAME_HTML = """<!doctype html><html><body>
<h1>Karriere</h1>
<iframe id="apply-frame" src="form.html" style="width:600px;height:400px"></iframe>
</body></html>"""

CAPTCHA_HTML = """<!doctype html><html><body>
<form>
  <input name="email"><input name="name">
  <div class="g-recaptcha" data-sitekey="fixture">recaptcha placeholder</div>
</form>
</body></html>"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed on this host")
class ProfileLockTest(unittest.TestCase):
    def test_second_acquire_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with profile_lock(Path(tmp)):
                with self.assertRaises(ProfileBusyError):
                    with profile_lock(Path(tmp)):
                        pass

    def test_stale_lock_is_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".apply-agent.lock").write_text("999999999")  # dead pid
            with profile_lock(Path(tmp)):  # must not raise
                pass

    def test_lock_released_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with profile_lock(Path(tmp)):
                pass
            with profile_lock(Path(tmp)):
                pass


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed on this host")
class HeadlessFlowTest(unittest.TestCase):
    """One shared headless session + fixture HTTP server for all flow tests."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "site").mkdir()
        for name, content in [
            ("detail.html", DETAIL_HTML), ("form.html", FORM_HTML),
            ("iframe.html", IFRAME_HTML), ("captcha.html", CAPTCHA_HTML),
        ]:
            (root / "site" / name).write_text(content, encoding="utf-8")

        handler = partial(SimpleHTTPRequestHandler, directory=str(root / "site"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

        cls.session_cm = headless_session(profile_dir=root / "profile")
        cls.context = cls.session_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.session_cm.__exit__(None, None, None)
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.page = self.context.new_page()

    def tearDown(self):
        self.page.close()

    def test_cookie_declined_and_apply_followed(self):
        report = goto_apply_page(self.page, f"{self.base}/detail.html")
        self.assertIn("Nur notwendige", report["cookie_clicked"])
        self.assertEqual(self.page.title(), "")  # navigated away from detail
        self.assertIsNotNone(report["clicked_apply"])
        self.assertTrue(report["final_url"].endswith("form.html"))
        self.assertTrue(report["form_found"])
        self.assertEqual(report["controls"]["light"], 3)
        self.assertFalse(report["captcha"])
        self.assertIsNone(report["error"])

    def test_decline_button_chosen_over_accept_all(self):
        self.page.goto(f"{self.base}/detail.html")
        from utils.browser import dismiss_cookie_banner
        clicked = dismiss_cookie_banner(self.page)
        self.assertIn("Nur notwendige", clicked)
        self.assertEqual(self.page.title(), "declined")  # not 'accepted'

    def test_direct_form_page_no_apply_click_needed(self):
        report = goto_apply_page(self.page, f"{self.base}/form.html")
        self.assertTrue(report["form_found"])
        self.assertIsNone(report["clicked_apply"])

    def test_iframe_form_extracted_with_frame_path(self):
        report = goto_apply_page(self.page, f"{self.base}/iframe.html")
        self.assertTrue(report["form_found"])  # controls counted across frames
        tree = extract_form_tree(self.page)
        self.assertEqual(tree["frames"], ["iframe#apply-frame"])
        names = {f.name for f in tree["fields"]}
        self.assertEqual(names, {"first_name", "email", "cv"})
        for f in tree["fields"]:
            self.assertEqual(f.frame_path, ("iframe#apply-frame",))
        self.assertIn('name="first_name"', tree["pruned"]["iframe#apply-frame"])

    def test_captcha_detected(self):
        report = goto_apply_page(self.page, f"{self.base}/captcha.html")
        self.assertTrue(report["captcha"])
        self.assertTrue(report["form_found"])  # form is still there — just flagged


LOGIN_TRAP_HTML = """<!doctype html><html><body>
<h1>Stellenanzeige — Mustermann GmbH</h1>
<form action="/login">
  <label>Benutzername <input name="user"></label>
  <label>E-Mail <input name="email" type="email"></label>
  <label>Telefon <input name="phone"></label>
  <label>Passwort <input name="pass" type="password"></label>
</form>
<a href="__ATS_FORM__">Zum Stellenportal</a>
</body></html>"""
# anchor text deliberately matches neither APPLY_BUTTON_PATTERNS nor
# /apply|bewerb/ — only the ATS-host pull-through can follow it

LOGIN_ONLY_HTML = LOGIN_TRAP_HTML.replace(
    '<a href="__ATS_FORM__">Zum Stellenportal</a>', "")


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed on this host")
class AtsPullThroughTest(unittest.TestCase):
    """A login form must not count as the application, and a known-ATS link
    on the page must be followed one more hop (lokale-kleinanzeigen lesson).
    Two servers so the ATS hop crosses netlocs like in the wild."""

    @classmethod
    def setUpClass(cls):
        import utils.browser as bw
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        for sub in ("board", "ats"):
            (root / sub).mkdir()
        (root / "ats" / "jobform.html").write_text(FORM_HTML, encoding="utf-8")

        def serve(sub):
            handler = partial(SimpleHTTPRequestHandler, directory=str(root / sub))
            srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return srv

        cls.board_srv, cls.ats_srv = serve("board"), serve("ats")
        ats_url = f"http://127.0.0.1:{cls.ats_srv.server_port}/jobform.html"
        (root / "board" / "trap.html").write_text(
            LOGIN_TRAP_HTML.replace("__ATS_FORM__", ats_url), encoding="utf-8")
        (root / "board" / "loginonly.html").write_text(
            LOGIN_ONLY_HTML, encoding="utf-8")
        cls.base = f"http://127.0.0.1:{cls.board_srv.server_port}"

        cls._ats_hosts = bw._ATS_HOSTS
        bw._ATS_HOSTS = ("127.0.0.1",)  # fixture server stands in for join.com
        cls.session_cm = headless_session(profile_dir=root / "profile")
        cls.context = cls.session_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        import utils.browser as bw
        bw._ATS_HOSTS = cls._ats_hosts
        cls.session_cm.__exit__(None, None, None)
        cls.board_srv.shutdown()
        cls.ats_srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.page = self.context.new_page()

    def tearDown(self):
        self.page.close()

    def test_login_form_is_rejected_and_ats_link_followed(self):
        report = goto_apply_page(self.page, f"{self.base}/trap.html")
        self.assertTrue(report["form_found"], report)
        self.assertTrue(report["final_url"].endswith("jobform.html"))
        self.assertIn("ats-pull-through", report["clicked_apply"] or "")
        self.assertEqual(report["controls"]["password"], 0)

    def test_login_form_without_ats_link_is_not_an_application(self):
        report = goto_apply_page(self.page, f"{self.base}/loginonly.html")
        self.assertFalse(report["form_found"])
        self.assertGreater(report["controls"]["password"], 0)  # account-wall


GONE_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<h1>Diese Stelle ist leider nicht mehr verfügbar.</h1>
</body></html>"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed on this host")
class GoneSignalTest(unittest.TestCase):
    """Vanished postings: explicit gone wording, or a same-site redirect
    from a deep job URL to the (locale) homepage (Zenjob/heyjobs lesson)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "site").mkdir()
        (root / "site" / "gone.html").write_text(GONE_HTML, encoding="utf-8")
        # search boxes on purpose: raw control counts must not mask the
        # redirect signal (the actual Zenjob/heyjobs failure mode)
        (root / "site" / "index.html").write_text(
            "<!doctype html><html><body><h1>Jobs Portal</h1>"
            '<input name="q"><input name="city"><input name="radius">'
            "</body></html>", encoding="utf-8")

        class Redirecting(SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/jobs/"):  # deep job URL → homepage
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                super().do_GET()

        handler = partial(Redirecting, directory=str(root / "site"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.session_cm = headless_session(profile_dir=root / "profile")
        cls.context = cls.session_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.session_cm.__exit__(None, None, None)
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.page = self.context.new_page()

    def tearDown(self):
        self.page.close()

    def test_gone_wording_is_detected(self):
        report = goto_apply_page(self.page, f"{self.base}/gone.html")
        self.assertFalse(report["form_found"])
        self.assertIn("gone-text", report["gone_signal"] or "")

    def test_deep_url_redirected_to_homepage_is_gone(self):
        report = goto_apply_page(self.page, f"{self.base}/jobs/dev-123")
        # the homepage's search boxes fool the raw control count — the
        # URL-based redirect signal must be set regardless
        self.assertEqual(report["gone_signal"], "redirected-to-homepage")


if __name__ == "__main__":
    unittest.main()
