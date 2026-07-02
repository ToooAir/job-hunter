"""apply_llm.py — shared LLM plumbing for Stage 1.

Once the home of the field-mapping/answering functions (map_pending_fields,
answer_open_questions); those were retired 2026-07-02 with the mapping chain
(see utils/apply_graph.py). What remains is the plumbing the verifier and the
orchestrator still use:

  _defaults            — lazy client/model resolution (tests inject fakes)
  _chat_json           — JSON-mode chat with one re-ask on unparseable output
  build_profile_facts  — candidate facts block for prompts
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
