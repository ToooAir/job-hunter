"""Tests for apply_session.py — gates, confirmation, dedup, watch matching,
plus container integration of the per-snapshot flow (prepare / submit /
drift give-up). Browser parts skip cleanly outside the container.

Fixture data is fictional (Max Mustermann policy).
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

import apply_session  # noqa: E402
from apply_session import (  # noqa: E402
    match_watched_snapshot,
    session_dedup_reason,
    snapshot_hosts,
    submit_gates,
    text_confirms,
)
from utils.db import create_application_snapshot, init_db  # noqa: E402
from utils.snapshot_io import approve_snapshot, fetch_work  # noqa: E402


class TextConfirmsTest(unittest.TestCase):
    def test_german_thank_you_pages(self):
        for text in ("Vielen Dank für Ihre Bewerbung!",
                     "Danke für deine Bewerbung",
                     "Ihre Bewerbung wurde erfolgreich übermittelt.",
                     "Die Bewerbung ist eingegangen"):
            self.assertTrue(text_confirms(text), text)

    def test_english_thank_you_pages(self):
        for text in ("Thank you for your application",
                     "Your application has been received.",
                     "You have successfully applied"):
            self.assertTrue(text_confirms(text), text)

    def test_ordinary_pages_do_not_confirm(self):
        for text in ("Jetzt bewerben", "Application form", "Danke fürs Lesen",
                     "", "Step 2 of 3: your details"):
            self.assertFalse(text_confirms(text), text)


class SubmitGatesTest(unittest.TestCase):
    def _snap(self, status="approved"):
        return {"status": status}

    def test_all_gates_open(self):
        ok, reasons = submit_gates(self._snap(), {"failed": 0}, False, True)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_each_gate_blocks(self):
        cases = [
            (self._snap("draft"), {"failed": 0}, False, True, "not approved"),
            (self._snap(), {"failed": 2}, False, True, "action(s) failed"),
            (self._snap(), {"failed": 0}, True, True, "captcha"),
            (self._snap(), {"failed": 0}, False, False, "prepare mode"),
        ]
        for snap, summary, captcha, flag, expected in cases:
            ok, reasons = submit_gates(snap, summary, captcha, flag)
            self.assertFalse(ok)
            self.assertTrue(any(expected in r for r in reasons), reasons)


class WatchMatchTest(unittest.TestCase):
    def _snap(self, apply_url, job_url=""):
        return {"id": 1, "apply_url": apply_url, "job": {"url": job_url}}

    def test_hosts_include_apply_and_job_urls(self):
        snap = self._snap("https://www.jobs.example.com/apply",
                          "https://example.com/job/1")
        self.assertEqual(snapshot_hosts(snap),
                         {"jobs.example.com", "example.com"})

    def test_match_strips_www_and_misses_other_hosts(self):
        watched = [self._snap("https://careers.mustermann.de/apply")]
        self.assertIsNotNone(
            match_watched_snapshot("www.careers.mustermann.de", watched))
        self.assertIsNone(match_watched_snapshot("other.example.com", watched))


class SessionDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.tmp.name) / "t.db"))
        for jid, company, status in [("a", "Mustermann GmbH", "scored"),
                                     ("b", "Mustermann SE", "applied"),
                                     ("c", "Beispiel AG", "scored")]:
            self.conn.execute(
                "INSERT INTO jobs (id, company, title, url, source,"
                " raw_jd_text, fetched_at, status)"
                " VALUES (?, ?, 't', ?, 's', ?, '2026-06-12', ?)",
                (jid, company, f"https://x.test/{jid}", f"jd {jid}", status))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_same_company_in_pipeline_is_flagged(self):
        snap = {"job_id": "a", "job": {"company": "Mustermann GmbH"}}
        reason = session_dedup_reason(self.conn, snap)
        self.assertIn("job b", reason)

    def test_own_job_and_other_companies_pass(self):
        self.assertIsNone(session_dedup_reason(
            self.conn, {"job_id": "b", "job": {"company": "Mustermann SE"}}))
        self.assertIsNone(session_dedup_reason(
            self.conn, {"job_id": "c", "job": {"company": "Beispiel AG"}}))


class RunBookTest(unittest.TestCase):
    """--book: manual booking for submissions watch couldn't attribute."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.db")
        conn = init_db(self.db)
        conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status) VALUES ('j1', 'Mustermann GmbH', 'Engineer',"
            " 'https://board.test/j1', 's', 'jd', '2026-06-12', 'scored')")
        self.sid = create_application_snapshot(
            conn, "j1", status="draft", tier=3, channel="no-form",
            apply_url="https://board.test/j1")
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _args(self, book):
        import argparse
        return argparse.Namespace(db=self.db, book=book)

    def test_books_tier3_draft_as_human_submission(self):
        apply_session.run_book(self._args(self.sid))
        row = self.conn.execute(
            "SELECT status, submitted_by, notes FROM application_snapshots"
            " WHERE id=?", (self.sid,)).fetchone()
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["submitted_by"], "human")
        self.assertIn("--book", row["notes"])
        job = self.conn.execute(
            "SELECT status, applied_at FROM jobs WHERE id='j1'").fetchone()
        self.assertEqual(job["status"], "applied")
        self.assertTrue(job["applied_at"])

    def test_unknown_snapshot_is_a_noop(self):
        apply_session.run_book(self._args(999))  # must not raise
        row = self.conn.execute(
            "SELECT status FROM application_snapshots WHERE id=?",
            (self.sid,)).fetchone()
        self.assertEqual(row["status"], "draft")


FORM_HTML = """<!doctype html><html><body>
<form action="danke.html" method="get">
  <label for="fn">Vorname</label><input id="fn" name="first_name">
  <label for="em">E-Mail</label><input id="em" type="email" name="email">
  <button type="submit">Bewerbung absenden</button>
</form>
</body></html>"""

DANKE_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<h1>Vielen Dank für Ihre Bewerbung!</h1>
</body></html>"""

PAYLOAD_OK = {"actions": [
    {"selector": "#fn", "kind": "text", "label": "Vorname", "action": "fill",
     "value": "Max", "source": "profile:first_name", "needs_review": False},
    {"selector": "#em", "kind": "email", "label": "E-Mail", "action": "fill",
     "value": "max.mustermann@example.com", "source": "profile:email",
     "needs_review": False}],
    "unfilled": [], "never_fill_skipped": []}

PAYLOAD_DRIFTED = {"actions": [
    PAYLOAD_OK["actions"][0],
    {"selector": "#fax", "kind": "text", "label": "Fax number",
     "action": "fill", "value": "000", "source": "profile:test",
     "needs_review": False}],
    "unfilled": [], "never_fill_skipped": []}


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed on this host")
class ProcessSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from utils.browser import headless_session
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "site").mkdir()
        (root / "site" / "form.html").write_text(FORM_HTML, encoding="utf-8")
        (root / "site" / "danke.html").write_text(DANKE_HTML, encoding="utf-8")
        handler = partial(SimpleHTTPRequestHandler, directory=str(root / "site"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.session_cm = headless_session(profile_dir=root / "profile")
        cls.context = cls.session_cm.__enter__()
        cls._shots = apply_session.SCREENSHOT_DIR
        apply_session.SCREENSHOT_DIR = root / "screenshots"
        import utils.form_executor as fx
        cls._timeout = fx.ACTION_TIMEOUT_MS
        fx.ACTION_TIMEOUT_MS = 800

    @classmethod
    def tearDownClass(cls):
        apply_session.SCREENSHOT_DIR = cls._shots
        import utils.form_executor as fx
        fx.ACTION_TIMEOUT_MS = cls._timeout
        cls.session_cm.__exit__(None, None, None)
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.dbtmp = tempfile.TemporaryDirectory()
        self.conn = init_db(str(Path(self.dbtmp.name) / "t.db"))
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status) VALUES ('j1', 'Mustermann GmbH',"
            " 'Backend Engineer', 'https://x.test/j1', 's', 'jd',"
            " '2026-06-12', 'scored')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.dbtmp.cleanup()

    def _approved(self, payload):
        sid = create_application_snapshot(
            self.conn, "j1", status="draft", tier=2, channel="company-form",
            apply_url=f"{self.base}/form.html", form_payload=payload)
        approve_snapshot(self.conn, sid)
        return fetch_work(self.conn)[0]

    def _status(self, sid):
        return self.conn.execute(
            "SELECT * FROM application_snapshots WHERE id=?", (sid,)).fetchone()

    def test_prepare_mode_fills_and_leaves_tab_open(self):
        snap = self._approved(PAYLOAD_OK)
        pages_before = len(self.context.pages)
        outcome = apply_session.process_snapshot(
            self.conn, self.context, snap, submit_flag=False)
        self.assertEqual(outcome, "prepared")
        self.assertEqual(len(self.context.pages), pages_before + 1)  # tab kept
        tab = self.context.pages[-1]
        self.assertEqual(tab.input_value("#fn"), "Max")
        row = self._status(snap["id"])
        self.assertEqual(row["status"], "approved")  # human may still submit
        self.assertIn("prepare mode", row["notes"])
        self.assertTrue(Path(self.tmp.name, "screenshots",
                             f"{snap['id']}.png").exists())
        tab.close()

    def test_submit_mode_submits_and_books_applied(self):
        snap = self._approved(PAYLOAD_OK)
        outcome = apply_session.process_snapshot(
            self.conn, self.context, snap, submit_flag=True)
        self.assertEqual(outcome, "submitted")
        row = self._status(snap["id"])
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["submitted_by"], "agent")
        self.assertIn("-submitted.png", row["screenshot_path"])
        job = self.conn.execute(
            "SELECT status, applied_at FROM jobs WHERE id='j1'").fetchone()
        self.assertEqual(job["status"], "applied")
        self.assertTrue(job["applied_at"])

    def test_unrecoverable_drift_books_failed(self):
        snap = self._approved(PAYLOAD_DRIFTED)  # 1/2 unfillable → >20%
        outcome = apply_session.process_snapshot(
            self.conn, self.context, snap, submit_flag=True)
        self.assertEqual(outcome, "failed")
        row = self._status(snap["id"])
        self.assertEqual(row["status"], "failed")
        self.assertIn("drift", row["notes"])
        from utils.db import get_in_flight_snapshots
        self.assertEqual(get_in_flight_snapshots(self.conn), [])  # re-queues


if __name__ == "__main__":
    unittest.main()
