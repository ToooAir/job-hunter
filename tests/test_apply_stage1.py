"""Tests for apply_stage1.py pure logic (verdicts, apply-form signature)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apply_stage1 import _has_apply_signature, verdict_of  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
