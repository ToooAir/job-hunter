"""Tests for utils/drift_recovery.py — label rematch + give-up rules.

Pure logic runs everywhere; the live-retry integration test needs
Playwright (container) and skips cleanly elsewhere.
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

from utils.dom_pruner import FormField  # noqa: E402
from utils.drift_recovery import (  # noqa: E402
    assess,
    recover_and_retry,
    rematch_action,
)
from utils.form_executor import execute_actions  # noqa: E402


def fld(selector, label, kind="text", required=False, frame=()):
    return FormField(selector=selector, frame_path=frame, kind=kind,
                     label=label, required=required)


def act(selector, label, kind="text", value="x", action="fill"):
    return {"selector": selector, "kind": kind, "label": label,
            "action": action, "value": value, "source": "profile:test",
            "needs_review": False}


class RematchTest(unittest.TestCase):
    def test_exact_label_and_kind_wins(self):
        live = [fld("#new-em", "E-Mail", kind="email"),
                fld("#decoy", "E-Mail", kind="checkbox")]
        fresh = rematch_action(act("#old-em", "E-Mail", kind="email"), live)
        self.assertEqual(fresh["selector"], "#new-em")
        self.assertEqual(fresh["value"], "x")  # approved value untouched

    def test_exact_label_any_kind_fallback(self):
        live = [fld("#new", "Vorname", kind="text")]
        fresh = rematch_action(act("#old", "Vorname", kind="custom"), live)
        self.assertEqual(fresh["selector"], "#new")

    def test_ambiguous_label_returns_none(self):
        live = [fld("#a", "Name"), fld("#b", "Name")]
        self.assertIsNone(rematch_action(act("#old", "Name"), live))

    def test_containment_match_needs_min_length(self):
        live = [fld("#new", "Gehaltsvorstellung (brutto)")]
        fresh = rematch_action(act("#old", "Gehaltsvorstellung"), live)
        self.assertEqual(fresh["selector"], "#new")
        self.assertIsNone(rematch_action(act("#old", "Ge"), live))

    def test_empty_label_returns_none(self):
        self.assertIsNone(rematch_action(act("#old", ""), [fld("#new", "")]))

    def test_frame_path_is_refreshed(self):
        live = [fld("#new", "Vorname", frame=("iframe#f",))]
        fresh = rematch_action(act("#old", "Vorname"), live)
        self.assertEqual(fresh["frame_path"], ["iframe#f"])


class AssessTest(unittest.TestCase):
    def _results(self, *oks):
        return [{"ok": ok, "error": None if ok else "x"} for ok in oks]

    def test_all_ok_is_none(self):
        self.assertIsNone(assess(self._results(True, True),
                                 [act("#a", "A"), act("#b", "B")], []))

    def test_required_failure_gives_up(self):
        live = [fld("#em", "E-Mail", required=True)]
        reason = assess(self._results(False, True),
                        [act("#em", "E-Mail"), act("#b", "B")], live)
        self.assertIn("required", reason)
        self.assertIn("E-Mail", reason)

    def test_exactly_20_percent_carries_on(self):
        actions = [act(f"#{i}", f"F{i}") for i in range(5)]
        self.assertIsNone(assess(self._results(False, True, True, True, True),
                                 actions, []))

    def test_over_20_percent_gives_up(self):
        actions = [act(f"#{i}", f"F{i}") for i in range(5)]
        reason = assess(self._results(False, False, True, True, True),
                        actions, [])
        self.assertIn(">20%", reason)


DRIFTED_FORM = """<!doctype html><html><body>
<form>
  <label for="fn-v2">Vorname</label><input id="fn-v2" name="first_name">
  <label for="em-v2">E-Mail</label><input id="em-v2" type="email" name="email" required>
</form>
</body></html>"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed on this host")
class LiveRecoveryTest(unittest.TestCase):
    """Stage 1 saw #fn / #em; the live page renamed them to *-v2."""

    @classmethod
    def setUpClass(cls):
        from utils.browser import headless_session
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "site").mkdir()
        (root / "site" / "drift.html").write_text(DRIFTED_FORM, encoding="utf-8")
        handler = partial(SimpleHTTPRequestHandler, directory=str(root / "site"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.session_cm = headless_session(profile_dir=root / "profile")
        cls.context = cls.session_cm.__enter__()
        import utils.form_executor as fx
        cls._timeout = fx.ACTION_TIMEOUT_MS
        fx.ACTION_TIMEOUT_MS = 800

    @classmethod
    def tearDownClass(cls):
        import utils.form_executor as fx
        fx.ACTION_TIMEOUT_MS = cls._timeout
        cls.session_cm.__exit__(None, None, None)
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.page = self.context.new_page()
        self.page.goto(f"{self.base}/drift.html")

    def tearDown(self):
        self.page.close()

    def test_stale_selectors_recover_by_label(self):
        actions = [act("#fn", "Vorname", value="Max"),
                   act("#em", "E-Mail", kind="email",
                       value="max.mustermann@example.com")]
        summary = execute_actions(self.page, actions)
        self.assertEqual(summary["failed"], 2)  # both selectors stale
        out = recover_and_retry(self.page, actions, summary)
        self.assertEqual(out["failed"], 0, out["results"])
        self.assertEqual(out["recovered"], 2)
        self.assertNotIn("give_up", out)
        self.assertEqual(self.page.input_value("#fn-v2"), "Max")
        self.assertEqual(self.page.input_value("#em-v2"),
                         "max.mustermann@example.com")

    def test_unmatchable_required_field_gives_up(self):
        # E-Mail is required on the live page and this stale action's label
        # matches it, but the field also fails after rematch? No — simulate
        # the harder case: the action's label exists nowhere live.
        actions = [act("#fn", "Vorname", value="Max"),
                   act("#fax", "Fax number", value="000")]
        summary = execute_actions(self.page, actions)
        out = recover_and_retry(self.page, actions, summary)
        self.assertEqual(out["failed"], 1)  # Vorname recovered, fax never will
        self.assertIn("give_up", out)       # 1/2 = 50% > 20%
        self.assertIn(">20%", out["give_up"])


if __name__ == "__main__":
    unittest.main()
