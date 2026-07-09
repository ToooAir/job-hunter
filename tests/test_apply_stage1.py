"""Tests for apply_stage1.py pure logic (verdicts, apply-form signature)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apply_stage1 import _has_apply_signature, is_unappliable, verdict_of  # noqa: E402
from utils.dom_pruner import FormField  # noqa: E402


def fields(*kinds):
    return [FormField(selector=f"#f{i}", kind=k) for i, k in enumerate(kinds)]


def report(**kw):
    base = {"error": None, "final_url": "https://example.com/apply",
            "captcha": False,
            "controls": {"textish": 0, "file": 0, "select": 0,
                         "checkbox_radio": 0, "shadow": 0, "light": 0,
                         "password": 0}}
    base.update(kw)
    return base


class TestApplySignature(unittest.TestCase):
    def test_file_or_textarea_qualifies(self):
        self.assertTrue(_has_apply_signature(fields("file")))
        self.assertTrue(_has_apply_signature(fields("textarea")))

    def test_name_plus_email_qualifies(self):
        self.assertTrue(_has_apply_signature(fields("text", "email")))

    def test_lonely_search_box_does_not(self):
        self.assertFalse(_has_apply_signature(fields("text")))  # Workato

    def test_email_only_newsletter_widget_does_not(self):
        self.assertFalse(_has_apply_signature(fields("email", "email", "checkbox")))  # Riverty


class TestVerdictOf(unittest.TestCase):
    def test_error_wins(self):
        self.assertEqual(verdict_of(report(error="timeout"), None), "nav-error")

    def test_external_board_by_final_url(self):
        r = report(final_url="https://www.xing.com/jobs/123")
        self.assertEqual(verdict_of(r, None), "external-board")

    def test_captcha_beats_form(self):
        r = report(captcha=True)
        tree = {"fields": fields("text", "email", "file")}
        self.assertEqual(verdict_of(r, tree), "captcha")

    def test_real_form_is_ok(self):
        self.assertEqual(
            verdict_of(report(), {"fields": fields("text", "email", "file")}), "ok")

    def test_junk_fields_are_weak_form(self):
        self.assertEqual(
            verdict_of(report(), {"fields": fields("text")}), "weak-form")

    def test_gone_signal_without_form_is_gone(self):
        r = report(gone_signal="redirected-to-homepage")
        self.assertEqual(verdict_of(r, None), "gone")

    def test_form_beats_gone_signal(self):
        r = report(gone_signal="gone-text: abgelaufen")
        tree = {"fields": fields("text", "email", "file")}
        self.assertEqual(verdict_of(r, tree), "ok")

    def test_pruned_empty_tree_with_redirect_is_gone(self):
        # homepage search boxes: raw form_found True, pruned tree empty
        r = report(gone_signal="redirected-to-homepage")
        self.assertEqual(verdict_of(r, {"fields": []}), "gone")

    def test_gone_beats_account_wall(self):
        r = report(gone_signal="redirected-to-homepage")
        r["controls"]["password"] = 1
        self.assertEqual(verdict_of(r, None), "gone")

    def test_password_without_form_is_account_wall(self):
        r = report()
        r["controls"]["password"] = 1
        self.assertEqual(verdict_of(r, None), "account-wall")

    def test_shadow_only(self):
        r = report()
        r["controls"].update(shadow=4, light=0)
        self.assertEqual(verdict_of(r, None), "shadow-only")

    def test_nothing_found_is_no_form(self):
        self.assertEqual(verdict_of(report(), None), "no-form")


class _FakeLink:
    def __init__(self, href, attached=True):
        self._href, self._attached = href, attached

    def wait_for(self, state="attached", timeout=0):
        if not self._attached:
            raise RuntimeError("link never hydrated")

    def get_attribute(self, name, timeout=0):
        return self._href


class _FakePage:
    """Playwright page stand-in for _heise_original. `links_by_page` maps a
    URL substring → {"originalanzeige": _FakeLink, "jetzt": _FakeLink}; goto()
    switches self.url, so the wizard's first page can carry different links
    than the detail page (the shape-2 layout)."""

    _NO_LINK = None  # sentinel: locator finds nothing → unattached link

    def __init__(self, links_by_page, current_url):
        self._by_page, self.url = links_by_page, current_url

    def goto(self, url, **kw):
        self.url = url

    def locator(self, *a, has_text=None, **kw):
        table = next((links for key, links in self._by_page.items()
                      if key in self.url), {})
        pat = has_text.pattern.lower() if has_text is not None else ""
        name = "originalanzeige" if "originalanzeige" in pat else "jetzt"
        link = table.get(name) or _FakeLink(None, attached=False)
        return type("L", (), {"first": link})()


class TestHeiseOriginal(unittest.TestCase):
    """_heise_original must reach the EXTERNAL link across all three heise
    apply shapes and fail closed otherwise — it must never return a target on
    heise's own application wizard."""

    DETAIL = "https://jobs.heise.de/job?id=1"
    WIZARD = ("https://jobs.heise.de/application?back=%2Fjob%3Fid%3D1"
              "&documentId=1&useCompanyForm=1")

    def setUp(self):
        import utils.browser as b
        self._b = b
        self._saved = (b.dismiss_cookie_banner, b._settle)
        b.dismiss_cookie_banner = lambda p: None  # browserless: no consent UI
        b._settle = lambda p: None

    def tearDown(self):
        self._b.dismiss_cookie_banner, self._b._settle = self._saved

    def _run(self, links_by_page):
        from apply_stage1 import _heise_original
        # the initial goto(url) navigates the fake page too, so the job URL
        # must be the detail URL the link tables are keyed on
        return _heise_original(_FakePage(links_by_page, self.DETAIL), self.DETAIL)

    def test_legacy_detail_page_originalanzeige_is_returned(self):
        url = "https://acme.softgarden.io/applications/x"
        self.assertEqual(self._run(
            {"/job": {"originalanzeige": _FakeLink(url)}}), url)

    def test_relative_redirect_endpoint_survives(self):
        # heise often points Originalanzeige at its own /redirect?... endpoint
        # that 302s out — a relative href must resolve, not be rejected.
        self.assertEqual(self._run(
            {"/job": {"originalanzeige": _FakeLink("/redirect?to=acme")}}),
            "https://jobs.heise.de/redirect?to=acme")

    def test_shape1_external_apply_button_is_returned(self):
        # 'Jetzt bewerben' href leaves heise directly
        url = "https://careers.acme.example/apply/42"
        self.assertEqual(self._run(
            {"/job": {"jetzt": _FakeLink(url)}}), url)

    def test_shape2_originalanzeige_inside_wizard_page_is_returned(self):
        # 2026-07-08 layout: the link moved INTO the application page (KHS probe)
        self.assertEqual(self._run({
            "/job?": {"jetzt": _FakeLink(self.WIZARD)},
            "/application": {"originalanzeige": _FakeLink(
                "https://germantechjobs.de/jobs/KHS-GmbH-Senior")},
        }), "https://germantechjobs.de/jobs/KHS-GmbH-Senior")

    def test_shape3_wizard_without_originalanzeige_returns_none(self):
        self.assertIsNone(self._run(
            {"/job?": {"jetzt": _FakeLink(self.WIZARD)}}))

    def test_heise_hosted_without_any_link_returns_none(self):
        self.assertIsNone(self._run({}))

    def test_loopback_to_heise_wizard_is_rejected(self):
        # a detail-page Originalanzeige that loops into the wizard is not external
        self.assertIsNone(self._run(
            {"/job": {"originalanzeige": _FakeLink(self.WIZARD)}}))

    def test_wizard_originalanzeige_looping_back_is_rejected(self):
        self.assertIsNone(self._run({
            "/job?": {"jetzt": _FakeLink(self.WIZARD)},
            "/application": {"originalanzeige": _FakeLink(self.WIZARD)},
        }))


class TestUnappliableGate(unittest.TestCase):
    """2026-07-08 abandoned-drafts review: verdicts that never converted get
    no draft — but addressable weak-forms must survive (Workato gh_jid: the
    probe under-extracts an iframe'd Greenhouse and calls it weak)."""

    def test_heise_own_form_is_always_gated(self):
        self.assertTrue(is_unappliable(
            "heise-own-form", {"ats": "unknown", "apply_url": None}))

    def test_weak_form_on_unknown_board_is_gated(self):
        # the jobware class: 6/6 such drafts died in the reviewer's hands
        self.assertTrue(is_unappliable(
            "weak-form", {"ats": "unknown",
                          "apply_url": "https://jobware.de/job/123"}))

    def test_weak_form_on_disguised_greenhouse_survives(self):
        self.assertFalse(is_unappliable(
            "weak-form", {"ats": "unknown",
                          "apply_url": "https://www.workato.com/careers?gh_jid=42"}))

    def test_weak_form_on_addressable_ats_survives(self):
        self.assertFalse(is_unappliable(
            "weak-form", {"ats": "personio", "apply_url": None}))

    def test_other_verdicts_pass(self):
        for verdict in ("ok", "captcha", "external-board", "no-form", "account-wall"):
            with self.subTest(verdict=verdict):
                self.assertFalse(is_unappliable(
                    verdict, {"ats": "unknown", "apply_url": None}))


class TestSkipUnappliable(unittest.TestCase):
    def setUp(self):
        import tempfile
        from utils.db import init_db
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "t.db"))
        for jid in ("j1", "j2"):
            self.conn.execute(
                "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
                " fetched_at, status) VALUES (?,?,?,?,?,?,?, 'scored')",
                (jid, "Mustermann GmbH", "Eng", f"https://x.com/{jid}", "t", "jd",
                 "2026-07-01T08:00:00"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _states(self):
        return [
            {"verdict": "heise-own-form",
             "job": {"id": "j1", "company": "A", "ats": "unknown", "apply_url": None}},
            {"verdict": "ok",
             "job": {"id": "j2", "company": "B", "ats": "unknown", "apply_url": None}},
        ]

    def test_marks_skipped_and_filters_states(self):
        from apply_stage1 import skip_unappliable
        keep = skip_unappliable(self.conn, self._states(), dry_run=False)
        self.assertEqual([s["job"]["id"] for s in keep], ["j2"])
        row = self.conn.execute(
            "SELECT status, notes FROM jobs WHERE id='j1'").fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertIn("un-appliable form (heise-own-form)", row["notes"])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM jobs WHERE id='j2'").fetchone()["status"], "scored")

    def test_dry_run_filters_but_writes_nothing(self):
        from apply_stage1 import skip_unappliable
        keep = skip_unappliable(self.conn, self._states(), dry_run=True)
        self.assertEqual(len(keep), 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM jobs WHERE id='j1'").fetchone()["status"], "scored")


if __name__ == "__main__":
    unittest.main()
