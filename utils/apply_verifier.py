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
  Tier 2 — anything a human must read first: LLM values, cover letter,
           custom answers, salary field (never auto-submit, decided
           2026-06-11), dedup warning, verifier not passed, required
           fields unfilled
  Tier 1 — fully deterministic payload, verifier passed, dedup ok
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
GENERATED parts of the draft. Check:
1. fabrication — any claim, number, or experience not supported by the background
2. salary — a salary statement that CONTRADICTS the candidate's stated
   expectation. Not mentioning salary is fine and must not be flagged.
3. visa / work permit — answers must match the background facts
4. language — generated text must be in English
5. oversharing — religion, marital status, photos or similarly sensitive
   topics must not be volunteered
Be strict about fabrication, lenient about phrasing and tone. Do not flag
omissions (information that could have been added but was not).
Respond with JSON only:
{"pass": <bool>, "issues": [{"where": "<cover_letter|question|field label>",
                             "issue": "<short>", "severity": "high|low"}]}"""


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
    llm_values = [
        {"field": a.get("label", ""), "value": a.get("value", "")}
        for a in draft.get("actions", [])
        if a.get("source") == "llm" and a.get("value")
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
            issues.append({"where": str(i.get("where", "")),
                           "issue": str(i["issue"]),
                           "severity": "high" if i.get("severity") == "high" else "low"})
    llm_pass = bool(out.get("pass"))
    final = llm_pass and not any(i["severity"] == "high" for i in issues)
    return {"pass": final, "issues": issues, "llm_checked": True}


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
    if draft.get("cover_letter"):
        reasons.append("cover letter present")
    if draft.get("custom_qa"):
        reasons.append("custom answers")
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

    return (2, reasons) if reasons else (1, [])
