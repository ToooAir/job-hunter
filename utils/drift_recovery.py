"""drift_recovery.py — selector refresh for drifted forms (Step 5.4).

Between Stage 1 (draft) and Stage 2 (session) a page can re-render with new
ids/structure: approved actions then fail on stale selectors. Recovery
re-extracts the LIVE field table and rematches the approved values by label
— no LLM call, no new content, so the reviewed-content red line stays
intact. Only the addressing is refreshed; values never change.

Give-up rules (user-set, 2026-06-12): after recovery
  * ANY required field still unfillable        → give up, or
  * more than 20% of actions still unfillable  → give up
Giving up means the caller books the snapshot 'failed'; the job re-queues,
Stage 1 regenerates against the live page, and the draft is re-reviewed —
that loop is the real fix for heavy drift.
"""

from __future__ import annotations

GIVE_UP_RATIO = 0.20
_MIN_FUZZY_LEN = 4


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold().rstrip("*").strip()


def rematch_action(action: dict, live_fields: list) -> dict | None:
    """The approved action re-addressed onto the live field table, or None.

    Match by label: exact (same kind) → exact (any kind) → containment.
    Anything ambiguous (two live fields share the label) is None — guessing
    between fields is how wrong data ends up in wrong boxes."""
    want = _norm(action.get("label"))
    if not want:
        return None
    exact = [f for f in live_fields if _norm(f.label) == want]
    candidates = [f for f in exact if f.kind == action.get("kind")] or exact
    if not candidates and len(want) >= _MIN_FUZZY_LEN:
        candidates = [
            f for f in live_fields
            if len(_norm(f.label)) >= _MIN_FUZZY_LEN
            and (want in _norm(f.label) or _norm(f.label) in want)
        ]
    if len(candidates) != 1:
        return None
    field = candidates[0]
    fresh = dict(action)
    fresh["selector"] = field.selector
    if field.frame_path:
        fresh["frame_path"] = list(field.frame_path)
    else:
        fresh.pop("frame_path", None)
    return fresh


def _is_required_live(action: dict, live_fields: list) -> bool:
    want = _norm(action.get("label"))
    return any(f.required for f in live_fields if want and _norm(f.label) == want)


def assess(results: list[dict], actions: list[dict],
           live_fields: list) -> str | None:
    """Post-recovery verdict: a give-up reason, or None to carry on."""
    failed = [(a, r) for a, r in zip(actions, results) if not r["ok"]]
    if not failed:
        return None
    required = [a.get("label") or a.get("selector")
                for a, _ in failed if _is_required_live(a, live_fields)]
    if required:
        return f"required field unfillable: {', '.join(required)}"
    if len(failed) / len(results) > GIVE_UP_RATIO:
        return (f"{len(failed)}/{len(results)} actions unfillable "
                f"after selector refresh (>20%)")
    return None


def recover_and_retry(page, actions: list[dict], summary: dict) -> dict:
    """Retry failed actions with live-rematched selectors; apply give-up rules.

    Returns the executor summary shape plus optional 'give_up' (reason str)
    and 'recovered' (count). Approved values are reused verbatim."""
    from utils.browser import extract_form_tree
    from utils.form_executor import execute_action

    results = list(summary["results"])
    live_fields = extract_form_tree(page)["fields"]
    recovered = 0
    for i, (action, res) in enumerate(zip(actions, results)):
        if res["ok"]:
            continue
        fresh = rematch_action(action, live_fields)
        if fresh is None:
            continue
        if (fresh["selector"] == action.get("selector")
                and fresh.get("frame_path") == action.get("frame_path")):
            continue  # same address — the failure wasn't drift
        retry = execute_action(page, fresh)
        if retry["ok"]:
            retry["recovered_selector"] = fresh["selector"]
            results[i] = retry
            recovered += 1

    failed = sum(1 for r in results if not r["ok"])
    out = {"results": results, "ok": len(results) - failed, "failed": failed,
           "recovered": recovered}
    reason = assess(results, actions, live_fields)
    if reason:
        out["give_up"] = reason
    return out
