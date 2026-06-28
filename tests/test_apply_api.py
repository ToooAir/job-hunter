"""Tests for apply_api.py — the extension's local sidecar.

Needs fastapi (present in the pipeline container); skips cleanly on the host
venv. Fixture data is fictional (Max Mustermann policy).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from utils.db import create_application_snapshot, init_db  # noqa: E402

TOKEN = "test-token-123"
PAYLOAD = {"actions": [{"selector": "#fn", "kind": "text", "label": "First Name",
                        "action": "fill", "value": "Max",
                        "source": "profile:first_name"}],
           "unfilled": [], "never_fill_skipped": []}


@unittest.skipUnless(HAS_FASTAPI, "fastapi not installed on this host")
class ApplyApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["DB_PATH"] = self.db_path
        os.environ["APPLY_API_TOKEN"] = TOKEN
        self.conn = init_db(self.db_path)
        self.conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
            " fetched_at, status, match_score, fit_grade, ats)"
            " VALUES ('job-a', 'Mustermann GmbH', 'Backend Engineer',"
            " 'https://example.com/job-a', 'test', 'jd', '2026-06-12T08:00:00',"
            " 'scored', 80, 'A', 'greenhouse')")
        self.conn.commit()
        self.sid = create_application_snapshot(
            self.conn, "job-a", status="draft", tier=2, channel="company-form",
            apply_url="https://job-boards.greenhouse.io/x/jobs/1", form_payload=PAYLOAD,
            cover_letter="Dear team, ...",
            custom_qa=[{"question": "Why us?", "answer": "Because."}])

        import apply_api
        self.apply_api = apply_api
        # point the CV at a temp file so we never touch the real profile
        # (replaces the lru_cached loader entirely, so no cache_clear needed)
        self.cv = Path(self.tmp.name) / "cv.pdf"
        self.cv.write_bytes(b"%PDF-1.4 fake cv")
        apply_api._cv_path = lambda: self.cv  # type: ignore[assignment]
        self.client = TestClient(apply_api.app)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("DB_PATH", None)
        os.environ.pop("APPLY_API_TOKEN", None)

    def _auth(self):
        return {"Authorization": f"Bearer {TOKEN}"}

    # ── auth ────────────────────────────────────────────────────────────────
    def test_missing_token_is_401(self):
        self.assertEqual(self.client.get("/pending").status_code, 401)

    def test_wrong_token_is_401(self):
        r = self.client.get("/pending", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_unset_server_token_is_503(self):
        os.environ.pop("APPLY_API_TOKEN", None)
        self.assertEqual(self.client.get("/pending", headers=self._auth()).status_code, 503)

    # ── endpoints ───────────────────────────────────────────────────────────
    def test_pending_lists_draft_with_parsed_host(self):
        r = self.client.get("/pending", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_id"], self.sid)
        self.assertEqual(rows[0]["company"], "Mustermann GmbH")
        self.assertEqual(rows[0]["host"], "job-boards.greenhouse.io")
        self.assertEqual(rows[0]["ats"], "greenhouse")

    def test_snapshot_returns_payload_and_letter(self):
        r = self.client.get(f"/snapshot/{self.sid}", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["form_payload"]["actions"][0]["value"], "Max")
        self.assertEqual(body["cover_letter"], "Dear team, ...")
        self.assertEqual(body["custom_qa"][0]["answer"], "Because.")

    def test_snapshot_unknown_is_404(self):
        self.assertEqual(
            self.client.get("/snapshot/9999", headers=self._auth()).status_code, 404)

    def test_cv_returns_pdf_bytes(self):
        r = self.client.get(f"/snapshot/{self.sid}/cv", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_submitted_books_job_and_is_idempotent(self):
        r = self.client.post(f"/snapshot/{self.sid}/submitted", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        row = self.conn.execute(
            "SELECT status, submitted_by FROM application_snapshots WHERE id=?",
            (self.sid,)).fetchone()
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["submitted_by"], "human")
        job = self.conn.execute(
            "SELECT status FROM jobs WHERE id='job-a'").fetchone()
        self.assertEqual(job["status"], "applied")
        # a second submit is an illegal transition → 409
        r2 = self.client.post(f"/snapshot/{self.sid}/submitted", headers=self._auth())
        self.assertEqual(r2.status_code, 409)


if __name__ == "__main__":
    unittest.main()
