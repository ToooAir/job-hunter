"""Tests for utils/apply_verifier.py (Step 4.3) — fake client, no network.

Fixtures use fictional Max Mustermann data only.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_verifier import _generated_parts, assign_tier, verify_draft  # noqa: E402
from utils.profile_loader import CandidateProfile  # noqa: E402
from tests.test_apply_llm import FakeClient  # noqa: E402

PROFILE = CandidateProfile({
    "meta": {"cv_path": "candidate_kb/cv/cv.pdf"},
    "fields": {
        "first_name": {"value": "Max", "aliases": ["vorname"]},
        "salary_expectation": {
            "value": "€70,000 gross per year (negotiable)", "value_eur_year": 70000,
            "aliases": ["gehaltsvorstellung"],
        },
    },
    "never_fill": ["religion / konfession"],
})

JOB = {"title": "Backend Engineer", "company": "Mustermann GmbH", "ats": "unknown"}


def act(**kw):
    base = {"selector": "#x", "kind": "text", "label": "", "action": "fill",
            "value": "", "source": "profile:first_name", "needs_review": False}
    base.update(kw)
    return base


def report(passed=True, issues=()):
    return json.dumps({"pass": passed, "issues": list(issues)})


class TestVerifyDraft(unittest.TestCase):
    def test_deterministic_only_when_nothing_generated(self):
        client = FakeClient([])  # raises if called
        out = verify_draft({"actions": [act(value="Max")]}, PROFILE, JOB,
                           client=client, model="m")
        self.assertTrue(out["pass"])
        self.assertFalse(out["llm_checked"])

    def test_salary_divergence_is_high_issue(self):
        draft = {"actions": [act(label="Gehaltsvorstellung", value="€90,000",
                                 source="profile:salary_expectation")]}
        out = verify_draft(draft, PROFILE, JOB, client=FakeClient([]), model="m")
        self.assertFalse(out["pass"])
        self.assertEqual(out["issues"][0]["severity"], "high")

    def test_salary_bare_number_is_allowed(self):
        draft = {"actions": [act(label="Gehalt", value="70000",
                                 source="profile:salary_expectation")]}
        out = verify_draft(draft, PROFILE, JOB, client=FakeClient([]), model="m")
        self.assertTrue(out["pass"])

    def test_never_fill_leak_is_high_issue(self):
        draft = {"actions": [act(label="Konfession", value="x", source="llm")]}
        out = verify_draft(draft, PROFILE, JOB,
                           client=FakeClient([report(True)]), model="m")
        self.assertFalse(out["pass"])
        self.assertTrue(any("never_fill" in i["issue"] for i in out["issues"]))

    def test_generated_content_sent_to_llm_reviewer(self):
        client = FakeClient([report(True)])
        draft = {"cover_letter": "I built RAG systems.",
                 "custom_qa": [{"question": "Why?", "answer": "Because RAG."}]}
        out = verify_draft(draft, PROFILE, JOB, kb_context="KB facts",
                           client=client, model="m")
        self.assertTrue(out["pass"])
        self.assertTrue(out["llm_checked"])
        user_msg = client.calls[0]["messages"][1]["content"]
        self.assertIn("KB facts", user_msg)
        self.assertIn("Because RAG.", user_msg)

    def test_llm_fail_verdict_fails_draft(self):
        client = FakeClient([report(False, [{"where": "cover_letter",
                                             "issue": "fabricated award",
                                             "severity": "high"}])])
        out = verify_draft({"cover_letter": "Award-winning engineer."},
                           PROFILE, JOB, client=client, model="m")
        self.assertFalse(out["pass"])
        self.assertIn("fabricated award", [i["issue"] for i in out["issues"]])

    def test_verifier_error_fails_closed(self):
        client = FakeClient(["not json", "still not json"])
        out = verify_draft({"cover_letter": "text"}, PROFILE, JOB,
                           client=client, model="m")
        self.assertFalse(out["pass"])
        self.assertTrue(any("verifier-error" in i["issue"] for i in out["issues"]))

    def test_low_issues_do_not_fail_a_passing_draft(self):
        client = FakeClient([report(True, [{"where": "cover_letter",
                                            "kind": "language",
                                            "issue": "generic phrasing",
                                            "severity": "low"}])])
        out = verify_draft({"cover_letter": "ok text"}, PROFILE, JOB,
                           client=client, model="m")
        self.assertTrue(out["pass"])
        self.assertEqual(len(out["issues"]), 1)
        self.assertEqual(out["issues"][0]["severity"], "low")

    def test_generated_parts_dedups_answer_and_field_value(self):
        # an open-question answer is both a custom_qa record and an llm fill
        # action — it must be audited once, not flagged twice.
        draft = {"custom_qa": [{"question": "Why?", "answer": "Because RAG."}],
                 "actions": [
                     {"label": "Why us", "value": "Because RAG.", "source": "llm"},
                     {"label": "Other", "value": "A different answer", "source": "llm"}]}
        fv = [v["value"] for v in _generated_parts(draft).get("field_values", [])]
        self.assertNotIn("Because RAG.", fv)      # already in "answers"
        self.assertIn("A different answer", fv)    # genuine extra value kept

    def test_misattribution_stays_low_and_passes(self):
        # real work credited to the wrong project is defensible — flag for
        # review (low), do not floor to high or fail the draft.
        client = FakeClient([report(True, [{"where": "answers",
                                            "kind": "misattribution",
                                            "issue": "VisaFlow feature, wrong project",
                                            "severity": "low"}])])
        out = verify_draft({"cover_letter": "ok"}, PROFILE, JOB,
                           client=client, model="m")
        self.assertTrue(out["pass"])
        self.assertEqual(out["issues"][0]["severity"], "low")
        self.assertEqual(out["issues"][0]["kind"], "misattribution")

    def test_fabrication_low_is_floored_to_high(self):
        # The reviewer tries to mark a fabricated metric "low"; the floor must
        # override so a false claim can never hide in the muted expander or
        # slip past the Tier gate, no matter what severity the LLM emits.
        client = FakeClient([report(True, [{"where": "cover_letter",
                                            "kind": "fabrication",
                                            "issue": "claims 100k users; not in background",
                                            "severity": "low"}])])
        out = verify_draft({"cover_letter": "Scaled the platform to 100k users."},
                           PROFILE, JOB, client=client, model="m")
        self.assertFalse(out["pass"])
        self.assertEqual(out["issues"][0]["severity"], "high")
        self.assertEqual(out["issues"][0]["kind"], "fabrication")


class TestAssignTier(unittest.TestCase):
    def test_bad_verdicts_are_tier3(self):
        for verdict in ("captcha", "external-board", "no-form", "account-wall"):
            tier, reasons = assign_tier(verdict, JOB, {})
            self.assertEqual(tier, 3, verdict)
            self.assertIn(verdict, reasons[0])

    def test_board_ats_is_tier3_even_with_ok_verdict(self):
        tier, _ = assign_tier("ok", {**JOB, "ats": "indeed"}, {})
        self.assertEqual(tier, 3)

    def test_pure_deterministic_draft_is_tier1(self):
        draft = {"actions": [act(value="Max"),
                             act(source="profile:cv", kind="file", value="cv.pdf")],
                 "unfilled": []}
        tier, reasons = assign_tier("ok", JOB, draft, {"pass": True})
        self.assertEqual((tier, reasons), (1, []))

    def test_email_gate_without_cv_is_not_tier1(self):
        # heise useCompanyForm / softgarden 'Firmen E-Mail' front door: fills a
        # field or two, no CV upload -> not a real application, must not auto-pass.
        draft = {"actions": [act(label="Firmen E-Mail", source="profile:email"),
                             act(label="E-Mail bestaetigen", source="profile:email")],
                 "unfilled": []}
        tier, reasons = assign_tier("ok", JOB, draft, {"pass": True})
        self.assertEqual(tier, 2)
        self.assertIn("no CV upload — form may be incomplete (wizard/email gate)",
                      reasons)

    def test_llm_values_force_tier2(self):
        draft = {"actions": [act(source="llm", needs_review=True)]}
        tier, reasons = assign_tier("ok", JOB, draft, {"pass": True})
        self.assertEqual(tier, 2)
        self.assertIn("llm-generated values", reasons)

    def test_bound_cover_letter_forces_tier2(self):
        # a cover letter the form actually has a slot for -> on the wire
        draft = {"actions": [act(source="cover_letter", value="Dear team")]}
        tier, reasons = assign_tier("ok", JOB, draft, {"pass": True})
        self.assertEqual(tier, 2)
        self.assertIn("cover letter on form", reasons)

    def test_unbound_cover_letter_does_not_block_tier1(self):
        # generated but no CL field on the form -> never submitted, so the
        # phantom letter must NOT demote an otherwise deterministic draft.
        draft = {"actions": [act(value="Max"),
                             act(source="profile:cv", kind="file", value="cv.pdf")],
                 "cover_letter": "Dear team", "unfilled": []}
        tier, reasons = assign_tier("ok", JOB, draft, {"pass": True})
        self.assertEqual((tier, reasons), (1, []))

    def test_salary_field_forces_tier2_even_deterministic(self):
        draft = {"actions": [act(source="profile:salary_expectation",
                                 value="70000")]}
        tier, reasons = assign_tier("ok", JOB, draft, {"pass": True})
        self.assertEqual(tier, 2)
        self.assertIn("salary field (never auto-submit)", reasons)

    def test_dedup_warn_forces_tier2(self):
        tier, _ = assign_tier("ok", JOB, {"actions": [act()]},
                              {"pass": True}, dedup="warn")
        self.assertEqual(tier, 2)

    def test_failed_verifier_forces_tier2(self):
        tier, reasons = assign_tier("ok", JOB, {"actions": [act()]},
                                    {"pass": False})
        self.assertEqual(tier, 2)
        self.assertIn("verifier not passed", reasons)

    def test_unfilled_required_forces_tier2(self):
        draft = {"actions": [act()],
                 "unfilled": [{"label": "Referenz", "required": True}]}
        tier, _ = assign_tier("ok", JOB, draft, {"pass": True})
        self.assertEqual(tier, 2)


if __name__ == "__main__":
    unittest.main()
