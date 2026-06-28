"""agentic_mapper.py — one-shot, whole-page LLM field mapping (Step 6.A probe).

The deterministic mapper (field_mapper) places one field at a time from rules;
this is the experimental alternative the user wanted to evaluate: serialize the
WHOLE page as ID-tagged text, let the LLM decide every control in a single
call, then ground its answers back onto the real selectors.

It produces the SAME `actions` shape field_mapper/apply_llm produce (so the
executor, verifier, tier rules and review UI consume it unchanged) and — like
all generated content — marks every action needs_review=True, so an agentic
draft can never reach Tier 1 / auto-submit.

The value of this module is as much its *validation layer* as its mapping: the
LLM's raw output is run through a strict grounding pass that catches
hallucinated ids, action/kind mismatches, placeholder picks and ungrounded
options. The diagnostics it records are what the offline probe scores.

LLM-only (no browser, no DB). utils.llm is imported lazily (host venv lacks it).
"""

from __future__ import annotations

import json

from utils.agentic_serialize import ground, serialize_fields
from utils.apply_llm import _chat_json, _defaults, _sanitize, build_profile_facts
from utils.field_mapper import _action, ground_option, is_placeholder_option

# Actions the LLM is allowed to emit, and the field kinds each is valid on.
_ACTION_KINDS = {
    "fill": {"text", "email", "tel", "url", "number", "date", "textarea", "custom"},
    "select_option": {"select", "radio"},
    "check": {"checkbox"},
    "upload": {"file"},
    "skip": None,  # valid on anything
}

_SYSTEM = """\
You are filling ONE job-application form for ONE candidate. You are shown the
form as a numbered list of controls — each line is `[id] kind "label" — flags`.

When a line carries `asks: "..."`, that is the control's REAL question (its
visible label was opaque) — answer that question, not the label. If you cannot
answer it truthfully from the facts, "skip" it; never paste an unrelated value.

For EVERY control output one decision, referencing it by its exact [id]:
- "fill": text/textarea/number/date/email/tel/custom field answerable from the
  candidate facts, OR a short free-text answer to a job question (motivation,
  notice period, salary, work eligibility). Ground every word in the facts —
  never invent. Keep free-text answers under 60 words.
- "select_option": for select/radio only. The value MUST be copied VERBATIM
  from THAT control's listed options. Never pick a placeholder like
  "Bitte wählen" / "Please select" / "Select..." / "Keine Angabe". If no real
  option fits the facts, use "skip".
- "check": checkbox — only when ticking it is truthful and in the candidate's
  interest (e.g. a required consent). Otherwise "skip".
- "upload": a file/attachment control asking for a CV / résumé / Lebenslauf.
  Use value "cv". Any other attachment → "skip".
- "skip": leave it empty. Use for search boxes, marketing opt-ins, fields about
  other people/companies, duplicate widget search inputs, or anything you
  cannot answer truthfully.

Never invent a control id that is not in the list. Never invent option values.
Respond with JSON only:
{"fields": [{"id": <int>, "action": "fill|select_option|check|upload|skip",
             "value": "<for fill/select_option/upload-cv>", "reason": "<short>"}]}"""


def _new_diag(total: int) -> dict:
    return {"total": total, "llm_rows": 0, "addressed": 0,
            "hallucinated_ids": [], "kind_mismatch": [], "placeholder_rejected": [],
            "option_not_grounded": [], "never_fill_blocked": [], "duplicate_ids": []}


def _unfilled(f: dict, reason: str) -> dict:
    return {"label": f.get("label", ""), "selector": f.get("selector", ""),
            "reason": reason, "required": bool(f.get("required"))}


def map_page_agentic(
    fields: list[dict],
    profile,
    job_meta: dict,
    client=None,
    model: str | None = None,
) -> dict:
    """Map an entire field table in one LLM call.

    Returns {actions, unfilled, custom_qa, diagnostics}. Every action carries
    needs_review=True. `diagnostics` is the per-field accounting the probe
    scores (hallucinations, mismatches, coverage).
    """
    result: dict = {"actions": [], "unfilled": [], "custom_qa": [],
                    "diagnostics": _new_diag(len(fields))}
    diag = result["diagnostics"]
    if not fields:
        return result

    client, model = _defaults(client, model)
    user = (
        f"Candidate facts:\n{build_profile_facts(profile)}\n\n"
        f"Job: {_sanitize(job_meta.get('title', ''))} at "
        f"{_sanitize(job_meta.get('company', ''))}\n\n"
        f"Form controls:\n{serialize_fields(fields)}"
    )
    out = _chat_json(client, model, _SYSTEM, user, max_tokens=2500)
    if out is None:
        result["unfilled"] = [_unfilled(f, "llm-error") for f in fields]
        return result

    rows = out.get("fields", []) if isinstance(out, dict) else []
    diag["llm_rows"] = len(rows)
    addressed: set[int] = set()
    cv_path = str(profile.meta.get("cv_path", "")) if profile else ""

    for row in rows:
        idx = row.get("id")
        f = ground(fields, idx)
        if f is None:
            diag["hallucinated_ids"].append(idx)
            continue
        if idx in addressed:
            diag["duplicate_ids"].append(idx)
            continue
        addressed.add(idx)

        action = str(row.get("action") or "").strip()
        value = str(row.get("value") or "")
        kind = f.get("kind", "text")
        label = f.get("label") or ""

        if action == "skip":
            result["unfilled"].append(_unfilled(f, "llm-skip"))
            continue

        valid_kinds = _ACTION_KINDS.get(action)
        if valid_kinds is None and action not in _ACTION_KINDS:
            diag["kind_mismatch"].append({"id": idx, "action": action, "kind": kind})
            result["unfilled"].append(_unfilled(f, f"bad-action:{action}"))
            continue
        if valid_kinds is not None and kind not in valid_kinds:
            diag["kind_mismatch"].append({"id": idx, "action": action, "kind": kind})
            result["unfilled"].append(_unfilled(f, f"kind-mismatch:{action}/{kind}"))
            continue

        # never-fill guard: a label on the profile's never_fill list is off
        # limits no matter how confident the LLM is.
        if profile and profile.is_never_fill(label):
            diag["never_fill_blocked"].append(idx)
            result["unfilled"].append(_unfilled(f, "never-fill"))
            continue

        if action == "select_option":
            grounded = _resolve_option(value, f.get("options") or [])
            if grounded is None:
                diag["option_not_grounded"].append({"id": idx, "value": value})
                result["unfilled"].append(_unfilled(f, "option-not-grounded"))
            elif is_placeholder_option(grounded):
                diag["placeholder_rejected"].append({"id": idx, "value": grounded})
                result["unfilled"].append(_unfilled(f, "llm-picked-placeholder"))
            else:
                result["actions"].append(
                    _action(f, "select_option", grounded, "agentic", True))
        elif action == "check":
            result["actions"].append(_action(f, "check", "", "agentic", True))
        elif action == "upload":
            if cv_path:
                result["actions"].append(_action(f, "upload", cv_path, "agentic:cv", True))
            else:
                result["unfilled"].append(_unfilled(f, "no-cv-path"))
        else:  # fill
            if not value.strip():
                result["unfilled"].append(_unfilled(f, "empty-fill"))
            else:
                result["actions"].append(_action(f, "fill", value, "agentic", True))
                if _looks_like_question(label):
                    result["custom_qa"].append(
                        {"question": label, "answer": value, "source": "agentic"})

    diag["addressed"] = len(addressed)
    for i, f in enumerate(fields):
        if i not in addressed:
            result["unfilled"].append(_unfilled(f, "llm-unaddressed"))
    return result


def _resolve_option(value: str, options: list[str]) -> str | None:
    """Verbatim option preferred; fall back to ground_option's synonym/substring
    matching so a near-miss ("Yes" vs "Ja") still lands instead of dropping."""
    if not value.strip():
        return None
    for o in options:
        if o.strip().casefold() == value.strip().casefold():
            return o
    return ground_option(value, options)


def _looks_like_question(label: str) -> bool:
    """A fill whose label reads like a question gets a custom_qa trail entry
    (interview-prep evidence), mirroring answer_open_questions."""
    text = (label or "").strip().lower()
    return text.endswith("?") or any(
        kw in text for kw in ("warum", "why", "motivat", "salary", "gehalt",
                              "kündigungs", "notice", "eligible", "berechtigt"))
