"""Tests for the Stage 1 per-job chain (utils/apply_graph.py).

Every test runs the real node bodies with a fake LLM client and dry_run
config (no DB, no network) — routing included: a verdict that must skip the
content chain proves it by making no LLM call at all, which is what the
recorder-node topology tests used to assert indirectly. Fictional Max
Mustermann data only.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_apply_llm import FakeClient  # noqa: E402
from utils.apply_graph import run_apply  # noqa: E402
from utils.profile_loader import CandidateProfile  # noqa: E402

PROFILE = CandidateProfile({
    "meta": {"cv_path": "candidate_kb/cv/cv.pdf"},
    "fields": {
        "first_name": {"value": "Max", "aliases": ["vorname"]},
    },
})


def config_for(client):
    return {"configurable": {"profile": PROFILE, "dry_run": True,
                             "client": client, "model": "m"}}


EMAIL_PROFILE = CandidateProfile({
    "meta": {"cv_path": "candidate_kb/cv/cv.pdf"},
    "fields": {
        "full_name": {"value": "Max Mustermann", "aliases": ["name"]},
        "email": {"value": "max.mustermann@example.com", "aliases": ["email"]},
        "github": {"value": "github.com/maxmustermann", "aliases": ["github"]},
        "german_level": {"value": "A2", "aliases": ["deutsch"]},
    },
})


class TestEmailOnly(unittest.TestCase):
    """email-only used to skip content entirely: the reviewer got the form
    cover letter to mail as-is, unverified (2026-10-14: wrong company name and
    an invented AI angle went out). It now gets a real email, audited."""

    JOB = {"id": "e1", "title": "Backend Engineer", "company": "Mustermann GmbH",
           "ats": "unknown", "dedup": "ok", "description": "Wir suchen ...",
           "cover_letter_draft": "Form letter."}
    BODY = "Your note on supportability stood out.\n\nMy CV is attached."

    def _run(self, **job):
        client = FakeClient([self.BODY, json.dumps({"pass": True, "issues": []})])
        cfg = {"configurable": {"profile": EMAIL_PROFILE, "dry_run": True,
                                "client": client, "model": "m"}}
        out = run_apply({"job": {**self.JOB, **job}, "verdict": "email-only",
                         "fields": [], "apply_url": "mailto:hr@example.com"}, cfg)
        return out, client

    def test_writes_and_verifies_an_email(self):
        out, client = self._run(jd_german=1)
        self.assertEqual(len(client.calls), 2)          # body + verifier
        self.assertEqual(out["tier"], 3)
        self.assertNotIn("Form letter", out["cover_letter"])
        self.assertTrue(out["verifier_report"]["pass"])
        # the verifier sees only the generated body: our German opener and the
        # signature must not be audited as claims (it flagged the opener live)
        audited = json.dumps(client.calls[1]["messages"], ensure_ascii=False)
        self.assertNotIn("Sehr geehrte", audited)
        self.assertNotIn("Kind regards", audited)
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("do not mention visa", system)    # shared CL rule rides along
        # live run volunteered "no completed CS degree" / "not three years"
        self.assertIn("Never raise degrees", system)

    def test_german_posting_opens_in_german_then_switches(self):
        out, _ = self._run(jd_german=1)
        email = out["cover_letter"]
        self.assertTrue(email.startswith("Sehr geehrte Damen und Herren,"))
        self.assertIn("Niveau A2", email)
        self.assertIn(self.BODY, email)

    def test_english_posting_and_deterministic_signature(self):
        out, _ = self._run(jd_german=0)
        email = out["cover_letter"]
        self.assertTrue(email.startswith("Dear Hiring Team,"))
        self.assertTrue(email.endswith("Kind regards,\nMax Mustermann\n"
                                       "max.mustermann@example.com\n"
                                       "github.com/maxmustermann"))   # no phone: skipped


class TestEndToEndDryRun(unittest.TestCase):
    """Real node bodies, fake LLM, dry_run — the full Pass B for one job."""

    JOB = {"id": "j9", "title": "Backend Engineer", "company": "Mustermann GmbH",
           "ats": "unknown", "dedup": "ok",
           "cover_letter_draft": "I build backends."}

    FIELDS = [
        {"selector": "#vn", "kind": "text", "label": "Vorname *", "required": True},
        {"selector": "#why", "kind": "textarea", "label": "Warum wir?"},
    ]

    def test_full_pipeline_produces_reviewed_tier2_draft(self):
        # mapping chain retired: no field actions, only the CL is carried and
        # audited; the completeness gate (no CV action) keeps the review floor
        client = FakeClient([
            json.dumps({"pass": True, "issues": []}),  # verifier audits the CL
        ])
        out = run_apply(
            {"job": self.JOB, "verdict": "ok", "fields": self.FIELDS,
             "apply_url": "https://example.com/apply"},
            config_for(client),
        )
        self.assertNotIn("actions", out)  # no fill payload is generated anymore
        self.assertEqual(out["tier"], 2)
        self.assertEqual(out["cover_letter"], "I build backends.")
        self.assertTrue(out["verifier_report"]["pass"])
        self.assertNotIn("snapshot_id", out)  # dry run wrote nothing
        self.assertEqual(out["apply_url"], "https://example.com/apply")

    def test_weak_form_fields_skip_mapping_chain(self):
        client = FakeClient([])  # junk field table must not reach the LLM
        out = run_apply(
            {"job": {**self.JOB, "id": "j11"}, "verdict": "weak-form",
             "fields": [{"selector": "#q", "kind": "text", "label": "Find a role"}]},
            config_for(client),
        )
        self.assertEqual(out["tier"], 3)
        self.assertEqual(client.calls, [])

    def test_no_fields_job_gets_tier3_without_llm_calls(self):
        client = FakeClient([])  # raises if any LLM call happens
        out = run_apply(
            {"job": {**self.JOB, "id": "j10"}, "verdict": "external-board",
             "fields": []},
            config_for(client),
        )
        self.assertEqual(out["tier"], 3)
        self.assertEqual(client.calls, [])
        # routing, stated as behaviour: the content chain never ran, so it
        # left neither a carried cover letter nor a verifier report behind
        self.assertNotIn("cover_letter", out)
        self.assertNotIn("verifier_report", out)

    def test_captcha_still_gets_the_content_chain(self):
        """captcha reads like a junk verdict but is not one — the page is real,
        the human just presses the button, so the draft is still worth
        generating and auditing."""
        client = FakeClient([json.dumps({"pass": True, "issues": []})])
        out = run_apply(
            {"job": {**self.JOB, "id": "j12"}, "verdict": "captcha",
             "fields": self.FIELDS},
            config_for(client),
        )
        self.assertEqual(out["cover_letter"], "I build backends.")
        self.assertTrue(out["verifier_report"]["pass"])


class TestSaveDraftStatus(unittest.TestCase):
    """Every saved draft starts in review — there is no auto-submission path."""

    def setUp(self):
        import tempfile
        from utils.db import init_db
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _save(self, tier, **cfg):
        from utils.apply_graph import save_draft
        state = {"job": {"id": "j1", "url": "https://x/apply"}, "tier": tier,
                 "verdict": "ok", "actions": [{"selector": "#a", "value": "Max"}],
                 "unfilled": [], "never_fill_skipped": []}
        out = save_draft(state, {"configurable": {"db_path": self.db_path, **cfg}})
        return self.conn.execute(
            "SELECT status, approved_at FROM application_snapshots WHERE id=?",
            (out["snapshot_id"],)).fetchone()

    def test_tier1_starts_as_draft(self):
        row = self._save(1)
        self.assertEqual(row["status"], "draft")
        self.assertIsNone(row["approved_at"])

    def test_tier2_starts_as_draft(self):
        row = self._save(2)
        self.assertEqual(row["status"], "draft")
        self.assertIsNone(row["approved_at"])


if __name__ == "__main__":
    unittest.main()
