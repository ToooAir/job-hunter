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

    def test_profile_cv_is_snapshot_free(self):
        # same CV, no snapshot id — the profile-fill mode's upload source
        r = self.client.get("/cv", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertEqual(self.client.get("/cv").status_code, 401)

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


PROFILE_FIXTURE = {
    "fields": {
        "first_name": {"value": "Max", "aliases": ["first name", "vorname"]},
        "email": {"value": "max@example.com", "aliases": ["email", "e-mail"]},
        "salary_expectation": {"value": "70000 EUR",
                               "aliases": ["salary expectation", "gehaltsvorstellung"]},
        "german_level": {"value": "B1", "aliases": ["german level"]},
        "earliest_start": {"value": "Immediately", "date_value": "+30 days",
                           "aliases": ["earliest start date"]},
    },
    "consents": {"auto_accept_aliases": ["i agree to the terms"]},
    "never_fill": ["date of birth / geburtsdatum", "gender"],
}


@unittest.skipUnless(HAS_FASTAPI, "fastapi not installed on this host")
class FillPlanTest(unittest.TestCase):
    """POST /fill-plan — snapshot-free fact fill (no DB/snapshot needed)."""

    def setUp(self):
        os.environ["APPLY_API_TOKEN"] = TOKEN
        import apply_api
        from utils.profile_loader import CandidateProfile
        self.apply_api = apply_api
        apply_api._profile = lambda: CandidateProfile(PROFILE_FIXTURE)  # type: ignore[assignment]
        self.client = TestClient(apply_api.app)

    def tearDown(self):
        os.environ.pop("APPLY_API_TOKEN", None)

    def _post(self, fields):
        return self.client.post("/fill-plan", json={"fields": fields},
                                headers={"Authorization": f"Bearer {TOKEN}"})

    def _plan(self, fields):
        r = self._post(fields)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_requires_token(self):
        self.assertEqual(self.client.post("/fill-plan", json={"fields": []}).status_code, 401)

    def test_element_id_echoed_back(self):
        # radio groups share one `name` — the client keys elements by `id`
        plan = self._plan([{"id": "jh-7", "label": "First Name", "name": "fn",
                            "type": "text"}])
        self.assertEqual(plan["fills"][0]["id"], "jh-7")

    def test_fact_matched_by_label(self):
        plan = self._plan([{"label": "First Name *", "name": "fn", "type": "text"}])
        self.assertEqual(len(plan["fills"]), 1)
        fill = plan["fills"][0]
        self.assertEqual(fill["value"], "Max")
        self.assertEqual(fill["action"], "fill")
        self.assertEqual(fill["source"], "profile:first_name")
        self.assertFalse(fill["needs_review"])

    def test_match_falls_back_to_input_name(self):
        # label is blank (join-style), but the input name carries the alias
        plan = self._plan([{"label": "", "name": "email", "type": "email"}])
        self.assertEqual(plan["fills"][0]["value"], "max@example.com")

    def test_unmatched_left_blank(self):
        plan = self._plan([{"label": "Describe your biggest failure", "name": "q1",
                            "type": "textarea"}])
        self.assertEqual(plan["fills"], [])
        self.assertEqual(len(plan["unmatched"]), 1)
        self.assertEqual(plan["unmatched"][0]["label"], "Describe your biggest failure")

    def test_never_fill_skipped_not_invented(self):
        plan = self._plan([{"label": "Date of birth", "name": "dob", "type": "text"},
                           {"label": "Gender", "name": "g", "type": "text"}])
        self.assertEqual(plan["fills"], [])
        self.assertEqual(len(plan["skipped_never_fill"]), 2)

    def test_consent_checkbox_auto_checked(self):
        plan = self._plan([{"label": "I agree to the terms and conditions",
                            "name": "c", "type": "checkbox"}])
        fill = plan["fills"][0]
        self.assertEqual(fill["action"], "check")
        self.assertTrue(fill["value"])
        self.assertEqual(fill["source"], "profile:consent")

    def test_select_resolves_to_real_option(self):
        plan = self._plan([{"label": "German level", "name": "de", "type": "select",
                            "options": ["A2", "B1 - intermediate", "C1"]}])
        fill = plan["fills"][0]
        self.assertEqual(fill["action"], "select_option")
        self.assertEqual(fill["value"], "B1 - intermediate")   # matched, not raw "B1"
        self.assertFalse(fill["needs_review"])

    def test_select_no_matching_option_flags_review(self):
        plan = self._plan([{"label": "German level", "name": "de", "type": "select",
                            "options": ["Bitte wählen", "Fließend"]}])
        fill = plan["fills"][0]
        self.assertTrue(fill["needs_review"])   # nothing fit → human decides
        self.assertEqual(fill["value"], "B1")   # value preserved, not silently wrong

    def test_date_field_resolves_to_concrete_date(self):
        plan = self._plan([{"label": "Earliest start date", "name": "start", "type": "text"}])
        # +30 days from today → an ISO date, not the literal "Immediately"
        value = plan["fills"][0]["value"]
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")

    def test_empty_fields_empty_plan(self):
        plan = self._plan([])
        self.assertEqual(plan, {"fills": [], "skipped_never_fill": [], "unmatched": []})


@unittest.skipUnless(HAS_FASTAPI, "fastapi not installed on this host")
class AnswerTest(unittest.TestCase):
    """POST /answer — dashboard-focus-first job resolution + grounded answer.

    Host matching must never guess: the motivating case is several drafts on
    one ATS host (Mistral ×3 on jobs.lever.co in the real cohort)."""

    def setUp(self):
        import json as _json
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["DB_PATH"] = self.db_path
        os.environ["APPLY_API_TOKEN"] = TOKEN
        self.conn = init_db(self.db_path)
        rows = [
            ("lever-1", "Mustermann AI", "Applied Lead",
             "https://x/1", "https://jobs.lever.co/must/1", "Own the FDE team."),
            ("lever-2", "Mustermann AI", "Platform Eng",
             "https://x/2", "https://jobs.lever.co/must/2", None),
            ("pers-1", "Beispiel GmbH", "Backend Eng",
             "https://x/3", "https://beispiel.jobs.personio.de/j/1", None),
        ]
        self.sids = {}
        for jid, comp, title, url, apply_url, cl in rows:
            self.conn.execute(
                "INSERT INTO jobs (id, company, title, url, source, raw_jd_text,"
                " fetched_at, status, cover_letter_draft, apply_url)"
                " VALUES (?,?,?,?, 'test', 'We build backends for Mustermann.',"
                " '2026-07-01T08:00:00', 'scored', ?, ?)",
                (jid, comp, title, url, cl, apply_url))
            self.sids[jid] = create_application_snapshot(
                self.conn, jid, status="draft", tier=2, apply_url=apply_url)
        self.conn.commit()

        import apply_api
        from utils.profile_loader import CandidateProfile
        self.apply_api = apply_api
        apply_api._profile = lambda: CandidateProfile({  # type: ignore[assignment]
            "fields": {"first_name": {"value": "Max", "aliases": ["vorname"]}}})
        self._json = _json
        self.client = TestClient(apply_api.app)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("DB_PATH", None)
        os.environ.pop("APPLY_API_TOKEN", None)

    def _llm_with(self, answer_json: str):
        from tests.test_apply_llm import FakeClient
        self.apply_api._llm = lambda: (FakeClient([answer_json]), "m")  # type: ignore

    def _ask(self, **body):
        return self.client.post(
            "/answer", json={"question": "Why us?", **body},
            headers={"Authorization": f"Bearer {TOKEN}"})

    def test_requires_token(self):
        self.assertEqual(
            self.client.post("/answer", json={"question": "x"}).status_code, 401)

    def test_focus_wins_and_writes_trail(self):
        from utils.db import set_focus
        set_focus(self.conn, self.sids["lever-1"], "lever-1")
        self._llm_with(self._json.dumps({"answer": "Because backends."}))
        body = self._ask(page_host="jobs.lever.co").json()
        self.assertEqual(body["grounding"]["via"], "focus")
        self.assertEqual(body["grounding"]["title"], "Applied Lead")
        self.assertEqual(body["grounding"]["kind"], "job+profile")
        self.assertEqual(body["warnings"], [])
        qa = self._json.loads(self.conn.execute(
            "SELECT custom_qa FROM application_snapshots WHERE id=?",
            (self.sids["lever-1"],)).fetchone()["custom_qa"])
        self.assertEqual(qa[-1]["answer"], "Because backends.")
        self.assertEqual(qa[-1]["source"], "on-demand")

    def test_ambiguous_host_never_guesses(self):
        # two lever drafts, no focus → profile-only + a set-the-focus hint
        self._llm_with(self._json.dumps({"answer": "Generic but honest."}))
        body = self._ask(page_host="jobs.lever.co").json()
        self.assertEqual(body["grounding"]["kind"], "profile-only")
        self.assertIsNone(body["grounding"]["via"])
        self.assertTrue(any("focus" in w for w in body["warnings"]))

    def test_unambiguous_host_matches(self):
        self._llm_with(self._json.dumps({"answer": "Grounded."}))
        body = self._ask(page_host="beispiel.jobs.personio.de").json()
        self.assertEqual(body["grounding"]["via"], "host")
        self.assertEqual(body["grounding"]["company"], "Beispiel GmbH")

    def test_focus_page_mismatch_warns_but_grounds_on_focus(self):
        from utils.db import set_focus
        set_focus(self.conn, self.sids["lever-1"], "lever-1")
        self._llm_with(self._json.dumps({"answer": "A."}))
        body = self._ask(page_host="beispiel.jobs.personio.de").json()
        self.assertEqual(body["grounding"]["via"], "focus")
        self.assertTrue(any("check before pasting" in w for w in body["warnings"]))

    def test_stale_focus_ignored(self):
        from utils.db import set_focus
        set_focus(self.conn, self.sids["pers-1"], "pers-1")
        self.conn.execute("UPDATE app_state SET updated_at='2020-01-01T00:00:00'")
        self.conn.commit()
        self._llm_with(self._json.dumps({"answer": "A."}))
        body = self._ask(page_host="beispiel.jobs.personio.de").json()
        self.assertEqual(body["grounding"]["via"], "host")  # fell through

    def test_llm_garbage_is_502_never_fabricated(self):
        from tests.test_apply_llm import FakeClient
        # garbage on both the ask and the re-ask → _chat_json gives up
        self.apply_api._llm = lambda: (  # type: ignore[assignment]
            FakeClient(["not json{{", "still not json{{"]), "m")
        r = self._ask(page_host="beispiel.jobs.personio.de")
        self.assertEqual(r.status_code, 502)

    def test_submitted_clears_matching_focus(self):
        from utils.db import get_focus, set_focus
        from utils.snapshot_io import mark_submitted
        sid = self.sids["pers-1"]
        set_focus(self.conn, sid, "pers-1")
        mark_submitted(self.conn, sid, note="t")
        self.assertIsNone(get_focus(self.conn))

    def test_submitted_keeps_moved_on_focus(self):
        from utils.db import get_focus, set_focus
        from utils.snapshot_io import mark_submitted
        set_focus(self.conn, self.sids["lever-1"], "lever-1")  # focus moved on
        mark_submitted(self.conn, self.sids["pers-1"], note="t")
        focus = get_focus(self.conn)
        self.assertIsNotNone(focus)
        self.assertEqual(focus["job_id"], "lever-1")


if __name__ == "__main__":
    unittest.main()
