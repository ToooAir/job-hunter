"""apply_llm.py — LLM mapping + content generation for Stage 1 (Step 4.2).

Two functions, both LLM-only (no browser, no DB):

  map_pending_fields    — classify the fields the deterministic mapper (4.1)
                          could not place: answer from profile facts, route
                          to open questions, skip, or hand to a human
  answer_open_questions — short grounded answers for custom questions,
                          written in English (decided 2026-06-10)

Shared rules:
  * utils.llm client, JSON mode (Mistral has no Structured Outputs),
    rate_limit() before every call
  * every LLM-provided value is needs_review=True — generated content never
    reaches a form without human eyes (the 4.3 tier rules enforce Tier 2)
  * accounting invariant: every input field/question lands in exactly one
    output bucket; LLM failures degrade to `unfilled`, never silently drop
  * transient API errors are NOT retried here — they bubble up so the
    orchestrator can record the job as failed and move on
"""

from __future__ import annotations

import json
import re

from utils.field_mapper import _action, is_placeholder_option

# utils.llm (and its openai import) is loaded lazily: tests inject a fake
# client, and the host venv intentionally lacks the LLM stack.


def _defaults(client, model):
    if client is None or model is None:
        from utils.llm import chat_model, make_client
        client = client or make_client()
        model = model or chat_model()
    return client, model

MAX_FIELDS_PER_CALL = 40
MAX_ANSWER_WORDS = 150
_COVER_LETTER_REASON = "cover-letter-slot"

# Process-wide LLM request counter (the orchestrator reports it per run).
CALL_STATS = {"calls": 0}

_MAP_SYSTEM = """\
You map job-application form fields to answers for one candidate.
Use ONLY the candidate facts provided. Never invent information.
For each field choose a decision:
- "value": answerable from the facts. For select/radio fields copy the value
  VERBATIM from the field's options; never pick placeholder entries such as
  "Bitte wählen", "Please select" or "--" — if no real option fits the facts,
  use "skip" or "needs_human" instead. For checkbox fields use value "check"
  only if ticking it is clearly in the candidate's interest and truthful.
- "open_question": the field wants a free-text, job-specific answer
  (motivation, experience, "why us"). Do not write the answer here.
- "skip": optional field best left empty (marketing opt-ins, referral codes,
  fields about other people or companies).
- "needs_human": required but not answerable from the facts, or a legal /
  sensitive declaration.
Respond with JSON only:
{"fields": [{"index": <int>, "decision": "value|open_question|skip|needs_human",
             "value": "<only for decision=value>", "reason": "<short>"}]}"""

_ANSWER_SYSTEM = f"""\
You write answers to job-application questions on behalf of one candidate.
Ground every claim in the candidate background provided — no invented facts,
no embellishment, no superlatives the background does not support.
NEVER add a concrete metric, number, percentage, dimension, version, or named
tool/provider that the background does not state: a plausible-sounding specific
you cannot point to in the background IS a fabrication. When the background
describes an achievement without a number, describe it without one.
Write in English even when the question is German. Be concise — at most
{MAX_ANSWER_WORDS} words per answer — and concrete only where the background is.
Respond with JSON only:
{{"answers": [{{"index": <int>, "answer": "<text>"}}]}}"""


def _sanitize(text: str) -> str:
    """Field labels/questions come from external pages — defang them."""
    return (text or "").replace("\x00", "").replace("<", "&lt;").replace(">", "&gt;")


def _chat_json(client, model, system, user, max_tokens=1500) -> dict | None:
    """JSON-mode chat with one re-ask on unparseable output."""
    from utils.llm import rate_limit
    for _ in range(2):
        rate_limit()
        CALL_STATS["calls"] += 1
        resp = client.chat.completions.create(
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


def _field_payload(pending: list[dict]) -> str:
    rows = []
    for i, f in enumerate(pending):
        row = {
            "index": i,
            "label": _sanitize(f.get("label", "")),
            "kind": f.get("kind", "text"),
            "required": bool(f.get("required")),
        }
        if f.get("context_hint"):
            # the control's REAL question, recovered when its visible label is
            # opaque (e.g. lever cards[uuid]). Without it the classifier and the
            # answerer work blind and confabulate a plausible answer from the KB.
            row["question"] = _sanitize(f["context_hint"])
        if f.get("options"):
            row["options"] = [_sanitize(o) for o in f["options"]]
        if f.get("suggestion") is not None:
            row["suggested_value"] = f["suggestion"]
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False)


def _unfilled(f: dict, reason: str) -> dict:
    return {"label": f.get("label", ""), "selector": f.get("selector", ""),
            "reason": reason, "required": bool(f.get("required"))}


def map_pending_fields(
    pending: list[dict],
    profile,
    job_meta: dict,
    client=None,
    model: str | None = None,
) -> dict:
    """Classify the deterministic mapper's leftovers.

    Returns {actions, open_questions, unfilled, cover_letter_slots}; the
    cover-letter slots never reach the LLM — the caller fills them with the
    reused letter (gen_content node).
    """
    result: dict = {"actions": [], "open_questions": [], "unfilled": [],
                    "cover_letter_slots": []}
    fields = []
    for f in pending:
        (result["cover_letter_slots"]
         if f.get("reason") == _COVER_LETTER_REASON else fields).append(f)
    if not fields:
        return result
    fields = fields[:MAX_FIELDS_PER_CALL]

    client, model = _defaults(client, model)
    user = (
        f"Candidate facts:\n{build_profile_facts(profile)}\n\n"
        f"Job: {_sanitize(job_meta.get('title', ''))} at "
        f"{_sanitize(job_meta.get('company', ''))}\n\n"
        f"Form fields:\n{_field_payload(fields)}"
    )
    out = _chat_json(client, model, _MAP_SYSTEM, user)
    if out is None:
        result["unfilled"] += [_unfilled(f, "llm-error") for f in fields]
        return result

    addressed = set()
    for row in out.get("fields", []):
        idx = row.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(fields) or idx in addressed:
            continue
        addressed.add(idx)
        f, decision = fields[idx], row.get("decision")
        value, reason = str(row.get("value") or ""), str(row.get("reason") or "")

        if decision == "value":
            if is_placeholder_option(value):
                # 'Bitte wählen' is in the options list, so the verbatim
                # guard alone would accept it — a placeholder is a non-answer
                # for any field kind (momox lesson)
                result["unfilled"].append(_unfilled(f, "llm-picked-placeholder"))
            elif (f.get("kind") in ("select", "radio")
                    and value not in (f.get("options") or [])):
                result["unfilled"].append(_unfilled(f, "llm-option-mismatch"))
            elif f.get("kind") == "checkbox":
                if value.strip().lower() == "check":
                    result["actions"].append(_action(f, "check", "", "llm", True))
                else:
                    result["unfilled"].append(_unfilled(f, f"llm-skip: {reason}"))
            else:
                act = "select_option" if f.get("kind") in ("select", "radio") else "fill"
                result["actions"].append(_action(f, act, value, "llm", True))
        elif decision == "open_question":
            # prefer the recovered real question over an opaque label, so the
            # answerer addresses the actual question instead of guessing from KB
            result["open_questions"].append(
                {**f, "question": f.get("context_hint") or f.get("label", "")})
        elif decision == "skip":
            result["unfilled"].append(_unfilled(f, f"llm-skip: {reason}"))
        else:  # needs_human or anything unrecognized
            result["unfilled"].append(_unfilled(f, f"needs-human: {reason}"))

    for i, f in enumerate(fields):
        if i not in addressed:
            result["unfilled"].append(_unfilled(f, "llm-unaddressed"))
    return result


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]) + "…"


def answer_open_questions(
    questions: list[dict],
    job_meta: dict,
    kb_context: str,
    client=None,
    model: str | None = None,
) -> dict:
    """Answer custom questions; returns {actions, custom_qa, unfilled}.

    Each answered question yields BOTH a fill action (the field is on the
    form) and a custom_qa record (the submission-evidence trail the snapshot
    keeps for interview prep)."""
    result: dict = {"actions": [], "custom_qa": [], "unfilled": []}
    if not questions:
        return result

    client, model = _defaults(client, model)
    qs = json.dumps(
        [{"index": i, "question": _sanitize(q.get("question") or q.get("label", ""))}
         for i, q in enumerate(questions)],
        ensure_ascii=False,
    )
    user = (
        f"Candidate background:\n{kb_context}\n\n"
        f"Job: {_sanitize(job_meta.get('title', ''))} at "
        f"{_sanitize(job_meta.get('company', ''))}\n\n"
        f"Questions:\n{qs}"
    )
    out = _chat_json(client, model, _ANSWER_SYSTEM, user, max_tokens=2500)
    answers = {}
    if out is not None:
        for row in out.get("answers", []):
            idx = row.get("index")
            if isinstance(idx, int) and 0 <= idx < len(questions) and row.get("answer"):
                answers[idx] = _truncate_words(str(row["answer"]).strip(), MAX_ANSWER_WORDS)

    for i, q in enumerate(questions):
        if i in answers:
            result["actions"].append(_action(q, "fill", answers[i], "llm", True))
            result["custom_qa"].append(
                {"question": q.get("question") or q.get("label", ""),
                 "answer": answers[i], "source": "llm"})
        else:
            result["unfilled"].append(_unfilled(q, "llm-error"))
    return result
