"""apply_verifier.py — independent draft check + tier assignment (Step 4.3).

verify_draft: deterministic checks (salary divergence, never_fill leakage,
cover-letter length) always run; an independent LLM reviewer additionally
audits GENERATED content (cover letter, custom answers, llm-sourced values)
against the candidate's real background — anti-embellishment is the whole
point. When nothing was generated, no LLM call is made. A verifier that
errors fails CLOSED: the draft is marked not-passed and lands in review.

assign_tier: pure rules, the user-approved table (2026-06-12):
  Tier 3 — Pass A verdict says the form is not fillable here (captcha,
           external board, account wall, ...) or the ATS is on the
           never-auto-submit list (join/indeed/stepstone)
  Tier 2 — anything a human must read first: LLM values, a cover letter
           BOUND to a form slot (a generated-but-unsubmitted letter does
           not count), salary field (never auto-submit, decided
           2026-06-11), dedup warning, verifier not passed, required
           fields unfilled
  Tier 1 — fully deterministic payload, verifier passed, dedup ok, AND a
           real application (has a CV upload — not a wizard/email gate)
"""

from __future__ import annotations

import json

# Pass A verdicts that mean "this form cannot be auto-filled here".
TIER3_VERDICTS = {
    "captcha", "external-board", "no-form", "weak-form", "shadow-only",
    "email-only", "nav-error", "account-wall", "probe-crash",
}
# Boards/ATS we never auto-submit on (account walls / ToS / ban risk).
TIER3_ATS = {"join", "indeed", "stepstone"}

MAX_CL_WORDS = 400

_VERIFY_SYSTEM = """\
You are an independent reviewer of a job-application draft written on behalf
of a candidate. You are given the candidate's REAL background and the
GENERATED parts of the draft. Tag every issue with the check it came from:
- "fabrication" — a claim, metric, or experience NOT present anywhere in the
  background (invented). A concrete number / metric / version / named tool the
  background does not contain IS fabrication, even if plausible.
- "misattribution" — REAL background work credited to the wrong project,
  company, or context (the candidate genuinely did it, the answer just places
  it elsewhere). Lower stakes than invention — flag it "low", not "high".
- "salary" — a salary statement that CONTRADICTS the candidate's stated
  expectation. Not mentioning salary is fine and must not be flagged.
- "visa" — work-permit answers that do not match the background facts.
- "language" — generated text not in English.
- "oversharing" — sensitive info (religion, marital status, health, photos)
  VOLUNTEERED in free text (cover letter / open answers) that no field asked
  for. A gender / diversity / disability question the FORM itself asks is
  prompted, not volunteered: answering it from the candidate's profile is
  NEVER oversharing — do not flag it.
Be strict about fabrication, lenient about phrasing and tone. Do not flag
omissions (information that could have been added but was not). fabrication,
salary, visa and oversharing issues are ALWAYS severity "high".
The employer name in the draft is taken from the job posting; the company
label given above may be a parent/holding company while the posting names the
operating subsidiary, or differ only by a legal-entity suffix. NEVER flag a
company / employer name difference — a posting-consistent employer name is
correct, not a fabrication.
Respond with JSON only:
{"pass": <bool>, "issues": [{"where": "<cover_letter|question|field label>",
                             "kind": "fabrication|misattribution|salary|visa|language|oversharing",
                             "issue": "<short>", "severity": "high|low"}]}"""

# Issue kinds that ship something false or harmful on the candidate's behalf —
# never let the LLM downgrade these to "low" where they'd hide in the muted
# expander and slip past the Tier gate. fabrication is the user's core concern.
_ALWAYS_HIGH = {"fabrication", "salary", "visa", "oversharing"}


def _deterministic_checks(draft: dict, profile) -> list[dict]:
    issues = []
    salary = profile.fields.get("salary_expectation")
    if salary:
        allowed = {salary.value, str(salary.extra.get("value_eur_year") or "")}
        for a in draft.get("actions", []):
            if (a.get("source") == "profile:salary_expectation"
                    and a.get("value") not in allowed):
                issues.append({"where": a.get("label", ""), "severity": "high",
                               "issue": "salary value diverges from profile"})
    for a in draft.get("actions", []):
        if a.get("label") and profile.is_never_fill(a["label"]):
            issues.append({"where": a["label"], "severity": "high",
                           "issue": "never_fill field has an action"})
    cl_words = len((draft.get("cover_letter") or "").split())
    if cl_words > MAX_CL_WORDS:
        issues.append({"where": "cover_letter", "severity": "low",
                       "issue": f"cover letter too long ({cl_words} words)"})
    return issues


def _generated_parts(draft: dict) -> dict:
    """Only generated content goes to the LLM reviewer — deterministic
    profile values need no fabrication check."""
    parts: dict = {}
    if draft.get("cover_letter"):
        parts["cover_letter"] = draft["cover_letter"]
    if draft.get("custom_qa"):
        parts["answers"] = [
            {"question": q.get("question", ""), "answer": q.get("answer", "")}
            for q in draft["custom_qa"]
        ]
    # an open-question answer is BOTH a fill action and a custom_qa record;
    # audit it once (under "answers"), or the same text gets flagged twice.
    answered = {q.get("answer", "") for q in draft.get("custom_qa", [])}
    llm_values = [
        {"field": a.get("label", ""), "value": a.get("value", "")}
        for a in draft.get("actions", [])
        if a.get("source") == "llm" and a.get("value")
        and a.get("value") not in answered
    ]
    if llm_values:
        parts["field_values"] = llm_values
    return parts


def verify_draft(
    draft: dict,
    profile,
    job_meta: dict,
    kb_context: str = "",
    client=None,
    model: str | None = None,
) -> dict:
    """Returns {"pass": bool, "issues": [...], "llm_checked": bool}."""
    issues = _deterministic_checks(draft, profile)
    parts = _generated_parts(draft)
    if not parts:
        return {"pass": not any(i["severity"] == "high" for i in issues),
                "issues": issues, "llm_checked": False}

    from utils.apply_llm import _chat_json, _defaults, build_profile_facts
    client, model = _defaults(client, model)
    user = (
        f"Candidate facts:\n{build_profile_facts(profile)}\n\n"
        f"Candidate background (knowledge base):\n{kb_context or '[none]'}\n\n"
        f"Job: {job_meta.get('title', '')} at {job_meta.get('company', '')}\n\n"
        f"Generated draft parts:\n{json.dumps(parts, ensure_ascii=False)}"
    )
    out = _chat_json(client, model, _VERIFY_SYSTEM, user, max_tokens=1200)
    if out is None:  # fail closed: unreviewable drafts go to a human
        issues.append({"where": "verifier", "severity": "high",
                       "issue": "verifier-error: no parseable response"})
        return {"pass": False, "issues": issues, "llm_checked": False}

    for i in out.get("issues", []):
        if isinstance(i, dict) and i.get("issue"):
            kind = str(i.get("kind", "")).strip().lower()
            severity = "high" if i.get("severity") == "high" else "low"
            if kind in _ALWAYS_HIGH:  # floor: never let a false claim hide as "low"
                severity = "high"
            issues.append({"where": str(i.get("where", "")),
                           "kind": kind,
                           "issue": str(i["issue"]),
                           "severity": severity})
    llm_pass = bool(out.get("pass"))
    final = llm_pass and not any(i["severity"] == "high" for i in issues)
    return {"pass": final, "issues": issues, "llm_checked": True}


def _has_cv_upload(actions: list[dict]) -> bool:
    """A real, complete application uploads a CV (a file field). An email-only
    gate / wizard step 1 — e.g. heise's useCompanyForm front door or
    softgarden's 'Firmen E-Mail' page — fills a field or two and nothing else.
    Requiring a CV upload before granting Tier 1 keeps those degenerate forms
    out of the auto-approve path; they fall to Tier 2 for a human to confirm."""
    return any(a.get("kind") == "file" or a.get("source") == "profile:cv"
               for a in actions)


def assign_tier(
    verdict: str,
    job: dict,
    draft: dict,
    verifier_report: dict | None = None,
    dedup: str = "ok",
) -> tuple[int, list[str]]:
    """The user-approved tier table; returns (tier, reasons)."""
    if verdict and verdict != "ok":
        return 3, [f"verdict: {verdict}"]
    if (job.get("ats") or "") in TIER3_ATS:
        return 3, [f"ats never auto-submitted: {job['ats']}"]

    reasons = []
    actions = draft.get("actions", [])
    if any(a.get("source") == "llm" for a in actions):
        reasons.append("llm-generated values")
    # Block auto-pass only on free text that WILL be SUBMITTED. A cover letter
    # is on the wire only when bound to a form slot (source="cover_letter", set
    # in apply_graph.fill_generated); a generated-but-unbound letter — the form
    # has no CL field — never reaches the employer, so it must not demote an
    # otherwise deterministic draft. Checking draft.get("cover_letter") (text
    # existence) was the "phantom cover letter" that made Tier 1 unreachable.
    # custom_qa answers, when present, are bound as source=="llm" actions and
    # are therefore already covered by the check above.
    if any(a.get("source") == "cover_letter" for a in actions):
        reasons.append("cover letter on form")
    if any(a.get("source") == "profile:salary_expectation" for a in actions):
        reasons.append("salary field (never auto-submit)")
    if dedup == "warn":
        reasons.append("dedup warning")
    report = verifier_report or {"pass": True}
    if not report.get("pass"):
        reasons.append("verifier not passed")
    if any(u.get("required") for u in draft.get("unfilled", [])):
        reasons.append("required field unfilled")
    if any(a.get("needs_review") for a in actions):
        reasons.append("actions need review")
    # Completeness gate (2026-06-17): Tier 1 auto-approves and a host --submit
    # session fires it WITHOUT a dashboard click, so it must be a real, complete
    # application — not a wizard step 1 / email gate. Only relevant to a draft
    # that is otherwise clean (no reason above); a CV upload is the signal.
    if not reasons and not _has_cv_upload(actions):
        reasons.append("no CV upload — form may be incomplete (wizard/email gate)")

    return (2, reasons) if reasons else (1, [])
