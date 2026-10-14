"""apply_llm.py — shared LLM plumbing for Stage 1.

Once the home of the field-mapping/answering functions (map_pending_fields,
answer_open_questions); those were retired 2026-07-02 with the mapping chain
(see utils/apply_graph.py). What remains is the plumbing the verifier and the
orchestrator still use:

  _defaults            — lazy client/model resolution (tests inject fakes)
  _chat_json           — JSON-mode chat with one re-ask on unparseable output
  build_profile_facts  — candidate facts block for prompts
  _sanitize            — defang external text before it enters a prompt
  CALL_STATS           — process-wide LLM request counter

Transient API errors are NOT retried here — they bubble up so the
orchestrator can record the job as failed and move on.
"""

from __future__ import annotations

import json

# utils.llm (and its openai import) is loaded lazily: tests inject a fake
# client, and the host venv intentionally lacks the LLM stack.


def _defaults(client, model):
    if client is None or model is None:
        from utils.llm import chat_model, make_client
        client = client or make_client()
        model = model or chat_model()
    return client, model


# Process-wide LLM request counter (the orchestrator reports it per run).
CALL_STATS = {"calls": 0}


def _sanitize(text: str) -> str:
    """Text from external pages (labels, pasted questions) — defang before it
    enters a prompt."""
    return (text or "").replace("\x00", "").replace("<", "&lt;").replace(">", "&gt;")


def _chat_json(client, model, system, user, max_tokens=1500) -> dict | None:
    """JSON-mode chat with one re-ask on unparseable output."""
    from utils.llm import rate_limit, chat_completion
    for _ in range(2):
        rate_limit()
        CALL_STATS["calls"] += 1
        resp = chat_completion(client,
            model=model,
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        try:
            out = json.loads(resp.choices[0].message.content)
            if isinstance(out, dict):
                return out
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def build_profile_facts(profile) -> str:
    """Candidate facts block for prompts; explanations ride along."""
    lines = []
    for f in profile.fields.values():
        note = f" (note: {f.explanation})" if f.explanation else ""
        lines.append(f"- {f.key}: {f.value}{note}")
    return "\n".join(lines)


# ── email-only applications ────────────────────────────────────────────────────
# The cover letter is a three-paragraph form attachment; mailed as-is it read
# as unsendable (2026-10-14). An email-only job gets its own short body. The
# greeting, the German-level line and the signature are deterministic: the LLM
# never writes German and never writes contact details.

_SIGNATURE_KEYS = ("phone", "email", "github", "linkedin")

_EMAIL_SYSTEM = """You write the body of a job application email on behalf of the candidate.
Output plain text only: no subject, no greeting, no sign-off, no signature, no markdown.
English, 110–220 words, 3–5 paragraphs of deliberately uneven length (one may be a
single sentence). It should read like a person wrote it to one company, not a form letter.
- Open on something specific in THIS posting — a responsibility, product, or problem the
  JD actually names — in your own words. Not "I am applying/writing", and not the stock
  "X caught my attention".
- One or two concrete achievements from the candidate background that fit the role.
- Optionally, if the JD names a technology, tool, or domain the background lacks, say so
  plainly in one sentence and name the closest real experience. Never raise degrees,
  years of experience, or language skills as a gap — those are screening criteria, not
  the candidate's to volunteer. Never refer to "the background" or "the candidate" —
  write in the first person as the candidate.
- Near the end, ask one question that matters for THIS role and that the JD leaves open;
  phrase it naturally rather than "Could you share which backend stack…".
- Close with one short line mentioning the attached CV, worded your own way.
- Do not mention the candidate's German level or language skills.
{grounding}"""


def email_greeting(german_posting: bool, german_level: str) -> str:
    if german_posting:
        level = f" auf Niveau {german_level}" if german_level else " noch im Aufbau"
        return ("Sehr geehrte Damen und Herren,\n\n"
                f"vielen Dank für Ihre Ausschreibung. Da mein Deutsch derzeit{level} ist, "
                "schreibe ich Ihnen im Folgenden auf Englisch.\n\n")
    return "Dear Hiring Team,\n\n"


def email_signature(profile) -> str:
    def val(key):
        f = profile.fields.get(key)
        return (f.value or "").strip() if f else ""
    lines = [val("full_name")] + [v for v in map(val, _SIGNATURE_KEYS) if v]
    return "Kind regards,\n" + "\n".join(line for line in lines if line)


def write_email_body(job: dict, kb_context: str, client=None, model=None) -> str:
    """The generated middle of the email — the only part the verifier audits."""
    from phase2_scorer import GROUNDING_RULES
    from utils.llm import chat_completion, rate_limit
    client, model = _defaults(client, model)
    user = (f"## Candidate Background\n{kb_context or '(none)'}\n\n"
            f"## Job\nCompany: {job.get('company')} | Title: {job.get('title')}\n\n"
            f"## Job Description\n<document>\n"
            f"{_sanitize((job.get('description') or '')[:6000])}\n</document>")
    rate_limit()
    CALL_STATS["calls"] += 1
    resp = chat_completion(client, model=model, temperature=0.6, messages=[
        {"role": "system", "content": _EMAIL_SYSTEM.format(grounding=GROUNDING_RULES)},
        {"role": "user", "content": user},
    ])
    return (resp.choices[0].message.content or "").strip()


def wrap_email(body: str, job: dict, profile) -> str:
    """Copy-ready email: deterministic greeting + body + signature. Applied
    after verification — the German opener is ours, not a generated claim."""
    if not body:
        return ""
    german_level = profile.fields["german_level"].value if "german_level" in profile.fields else ""
    return (email_greeting(bool(job.get("jd_german")), german_level)
            + body + "\n\n" + email_signature(profile))
