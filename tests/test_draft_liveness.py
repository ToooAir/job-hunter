"""Tests for utils/draft_liveness.py — the pending-draft liveness sweep.

Pure classifiers + persistence + orchestration with injected fakes (no network /
no browser). Fixture data is fictional (Max Mustermann policy).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import create_application_snapshot, init_db  # noqa: E402
from utils.draft_liveness import (  # noqa: E402
    apply_result,
    classify_http,
    liveness_from_verdict,
    sweep_drafts,
)


class PureClassifierTest(unittest.TestCase):
    def test_http_404_410_dead(self):
        self.assertEqual(classify_http("https://x.com/job/1", 404, "https://x.com/job/1"), "dead")
        self.assertEqual(classify_http("https://x.com/job/1", 410, None), "dead")

    def test_http_redirect_to_home_dead(self):
        # deep posting path collapsed to the bare host = taken down
        self.assertEqual(classify_http("https://x.com/job/1", 200, "https://x.com/"), "dead")
        self.assertEqual(classify_http("https://x.com/job/1", 200, "https://x.com"), "dead")

    def test_http_live_page_is_maybe(self):
        self.assertEqual(classify_http("https://x.com/job/1", 200, "https://x.com/job/1"), "maybe")
        # redirect that stays on a deep path is not "home" → still confirm headless
        self.assertEqual(classify_http("https://x.com/job/1", 200, "https://x.com/apply/1"), "maybe")

    def test_http_unknown(self):
        self.assertEqual(classify_http("https://x.com/job/1", 403, None), "unknown")
        self.assertEqual(classify_http("https://x.com/job/1", None, None), "unknown")
        self.assertEqual(classify_http("https://x.com/job/1", 503, None), "unknown")

    def test_verdict_mapping(self):
        self.assertEqual(liveness_from_verdict("ok"), "live")
        for dead in ("gone", "no-form"):
            self.assertEqual(liveness_from_verdict(dead), "dead")
        for sus in ("account-wall", "captcha", "weak-form", "nav-error", "external-board"):
            self.assertEqual(liveness_from_verdict(sus), "suspicious")


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.db")
        self.conn = init_db(self.db)
        for jid in ("ja", "jb", "jc"):
            self.conn.execute(
                "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
                " fetched_at, status) VALUES (?,?,?,?,?,?,?, 'scored')",
                (jid, "Mustermann GmbH", "Eng", f"https://x.com/{jid}", "t", "jd",
                 "2026-06-10T08:00:00"))
        self.conn.commit()
        self.sa = create_application_snapshot(self.conn, "ja", status="draft", tier=2,
                                              apply_url="https://x.com/ja")
        self.sb = create_application_snapshot(self.conn, "jb", status="draft", tier=2,
                                              apply_url="https://x.com/jb")
        self.sc = create_application_snapshot(self.conn, "jc", status="draft", tier=2,
                                              apply_url="https://x.com/jc")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _snap(self, sid):
        return self.conn.execute(
            "SELECT status, liveness FROM application_snapshots WHERE id=?", (sid,)).fetchone()

    def _job(self, jid):
        return self.conn.execute(
            "SELECT status, ats_checked_at FROM jobs WHERE id=?", (jid,)).fetchone()

    def test_dead_abandons_and_expires(self):
        apply_result(self.conn, self.sa, "ja", "dead", note="http 404")
        self.assertEqual(self._snap(self.sa)["status"], "abandoned")
        self.assertEqual(self._job("ja")["status"], "expired")

    def test_live_refreshes_and_flags(self):
        apply_result(self.conn, self.sb, "jb", "live", now="2026-06-26T10:00:00")
        self.assertEqual(self._snap(self.sb)["liveness"], "live")
        self.assertEqual(self._job("jb")["ats_checked_at"], "2026-06-26T10:00:00")
        self.assertEqual(self._snap(self.sb)["status"], "draft")  # still in queue

    def test_suspicious_flags_only(self):
        apply_result(self.conn, self.sc, "jc", "suspicious")
        self.assertEqual(self._snap(self.sc)["liveness"], "suspicious")
        self.assertEqual(self._snap(self.sc)["status"], "draft")
        self.assertEqual(self._job("jc")["status"], "scored")  # not expired


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.db")
        self.conn = init_db(self.db)
        self.sid = {}
        # the form-bearing drafts (go to headless) and one manual no-fill draft
        fill = {"actions": [{"selector": "#fn", "value": "Max"}]}
        payloads = {"dead404": fill, "live": fill, "gone": fill, "captcha": fill,
                    "manual": {"actions": []}}
        for jid, pl in payloads.items():
            self.conn.execute(
                "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
                " fetched_at, status) VALUES (?,?,?,?,?,?,?, 'scored')",
                (jid, f"{jid} GmbH", "Eng", f"https://x.com/{jid}", "t", "jd",
                 "2026-06-10T08:00:00"))
            self.sid[jid] = create_application_snapshot(
                self.conn, jid, status="draft", tier=2,
                apply_url=f"https://x.com/{jid}", form_payload=pl)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_two_stage_sweep(self):
        # HTTP stage: dead404 → 404 (clear-dead); the rest pass on.
        def fake_http(url):
            if url.endswith("/dead404"):
                return 404, url
            return 200, url

        # headless stage: only form-bearing, non-404 drafts reach here. A manual
        # (no-actions) draft must NOT — verdicts has no key for it, so a KeyError
        # would fail the test if it were wrongly sent to headless.
        verdicts = {"https://x.com/live": "ok", "https://x.com/gone": "gone",
                    "https://x.com/captcha": "captcha"}

        def fake_headless(drafts):
            for d in drafts:
                yield d, verdicts[d["apply_url"]]

        tally = sweep_drafts(self.conn, http_get=fake_http,
                             headless_verdicts=fake_headless)

        self.assertEqual(tally["checked"], 5)
        self.assertEqual(tally["dead"], 2)        # dead404 (http) + gone (form)
        self.assertEqual(tally["live"], 2)        # live (form) + manual (loads)
        self.assertEqual(tally["suspicious"], 1)  # captcha

        def status(jid):
            return self.conn.execute(
                "SELECT s.status ss, s.liveness lv, j.status js FROM application_snapshots s "
                "JOIN jobs j ON j.id=s.job_id WHERE s.id=?", (self.sid[jid],)).fetchone()

        self.assertEqual(status("dead404")["ss"], "abandoned")
        self.assertEqual(status("dead404")["js"], "expired")
        self.assertEqual(status("gone")["js"], "expired")
        self.assertEqual(status("live")["ss"], "draft")
        self.assertEqual(status("manual")["lv"], "live")     # live without headless
        self.assertEqual(status("captcha")["lv"], "suspicious")

    def test_ats_gone_kills_draft_even_when_page_loads(self):
        # Tier-3 zombie: apply_url is a generic careers page (always 200), but
        # ats_scan already saw the source listing 404 — must die without HTTP.
        self.conn.execute("UPDATE jobs SET ats='gone' WHERE id='manual'")
        self.conn.commit()
        verdicts = {"https://x.com/live": "ok", "https://x.com/gone": "gone",
                    "https://x.com/captcha": "captcha"}
        tally = sweep_drafts(
            self.conn, http_get=lambda u: (200, u) if not u.endswith("/dead404") else (404, u),
            headless_verdicts=lambda ds: ((d, verdicts[d["apply_url"]]) for d in ds))
        self.assertEqual(tally["dead"], 3)   # dead404 + gone + the ats-gone manual
        self.assertEqual(tally["live"], 1)   # only the live form draft
        row = self.conn.execute(
            "SELECT s.status ss, j.status js FROM application_snapshots s "
            "JOIN jobs j ON j.id=s.job_id WHERE s.id=?", (self.sid["manual"],)).fetchone()
        self.assertEqual(row["ss"], "abandoned")
        self.assertEqual(row["js"], "expired")

    def test_soft_gone_body_kills_manual_draft_without_headless(self):
        # The manual Tier-3 blind spot: the page answers 200 ("page loads" used
        # to pass as live) but its visible text says the posting is over.
        closed = "<html><body>This job is no longer accepting applications</body></html>"

        def fake_http(url):  # 3-tuple form: (status, final_url, body)
            body = closed if url.endswith("/manual") else "<h1>Jetzt bewerben</h1>"
            return 200, url, body

        verdicts = {"https://x.com/live": "ok", "https://x.com/gone": "gone",
                    "https://x.com/captcha": "captcha",
                    "https://x.com/dead404": "ok"}
        tally = sweep_drafts(
            self.conn, http_get=fake_http,
            headless_verdicts=lambda ds: ((d, verdicts[d["apply_url"]]) for d in ds))
        self.assertEqual(tally["dead"], 2)  # gone (form) + the soft-gone manual
        row = self.conn.execute(
            "SELECT s.status ss, s.notes, j.status js FROM application_snapshots s "
            "JOIN jobs j ON j.id=s.job_id WHERE s.id=?", (self.sid["manual"],)).fetchone()
        self.assertEqual(row["ss"], "abandoned")
        self.assertEqual(row["js"], "expired")
        self.assertIn("soft-gone", row["notes"])

    def test_dry_run_writes_nothing(self):
        tally = sweep_drafts(self.conn, http_get=lambda u: (404, u),
                             headless_verdicts=lambda ds: iter(()), dry_run=True)
        self.assertEqual(tally["dead"], 5)
        # nothing abandoned/expired
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM application_snapshots WHERE status='abandoned'").fetchone()["c"]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
