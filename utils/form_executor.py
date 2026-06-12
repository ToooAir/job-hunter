"""form_executor.py — execute a draft's form_payload on a live page (Step 5.0).

The form_payload contract (Step 4) is the instruction set; this module is
the machine that runs it. Five actions: fill / select_option / check /
upload / skip. Pure executor — no LLM, no DB, no navigation: apply_session
owns the browser lifecycle and the submit gates. Every action returns its
own {ok, error} result and one broken selector never aborts the batch;
deciding what a failure means (drift recovery, give up) is the caller's job.

Radio quirk (Step 5 contract): the payload value for a radio group is the
*option label*, not the input's value attribute — the group is resolved
live by name attribute + label text. The selector in the payload points at
one radio of the group (whichever the pruner saw first), which may not be
the one to check.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Short per-action cap: a drifted selector should fail fast, not eat the
# 30s navigation timeout once per action. Module-level so tests can lower it.
ACTION_TIMEOUT_MS = 5_000

_RADIO_LABEL_JS = """
el => {
  if (el.id) {
    const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lab) return lab.innerText;
  }
  const wrap = el.closest('label');
  if (wrap) return wrap.innerText;
  return el.getAttribute('aria-label') || '';
}
"""


def _scope(page, frame_path):
    """Descend the payload's iframe chain via frame_locator."""
    scope = page
    for sel in frame_path or ():
        scope = scope.frame_locator(sel)
    return scope


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def resolve_upload_path(value: str) -> Path:
    """Payload upload values are project-root-relative (profile contract)."""
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def _radio_candidates(scope, action):
    base = scope.locator(action["selector"]).first
    name = base.get_attribute("name", timeout=ACTION_TIMEOUT_MS)
    if not name:
        return [base]  # lone, nameless radio — all we can do is match itself
    return list(scope.locator(f'input[type="radio"][name="{name}"]').all())


def _do_radio(scope, action) -> str | None:
    """Check the group member whose label matches the payload value.
    Returns an error string or None on success."""
    want = _norm(action.get("value"))
    if not want:
        return "radio-empty-value"
    candidates = _radio_candidates(scope, action)
    labelled = []
    for radio in candidates:
        label = _norm(radio.evaluate(_RADIO_LABEL_JS))
        value_attr = _norm(radio.get_attribute("value"))
        labelled.append((radio, label, value_attr))
        if want in (label, value_attr):
            radio.check(timeout=ACTION_TIMEOUT_MS)
            return None
    if len(want) >= 3:  # relaxed pass, same spirit as ground_option's substring rule
        for radio, label, _ in labelled:
            if want in label:
                radio.check(timeout=ACTION_TIMEOUT_MS)
                return None
    seen = [lab or val for _, lab, val in labelled]
    return f"radio-option-not-found: {action.get('value')!r} not in {seen}"


def _do_fill(scope, page, action) -> None:
    loc = scope.locator(action["selector"]).first
    value = action.get("value") or ""
    if action.get("kind") == "custom":
        # custom widgets (react-select & friends): fill if the node accepts
        # it (contenteditable does), else click to focus and type
        try:
            loc.fill(value, timeout=ACTION_TIMEOUT_MS)
        except Exception:
            loc.click(timeout=ACTION_TIMEOUT_MS)
            page.keyboard.type(value, delay=20)
        return
    loc.fill(value, timeout=ACTION_TIMEOUT_MS)


def _do_select(scope, action) -> None:
    loc = scope.locator(action["selector"]).first
    value = action.get("value") or ""
    try:
        loc.select_option(label=value, timeout=ACTION_TIMEOUT_MS)
    except Exception:
        # mapper grounds against option text, but tolerate a payload that
        # carries the option's value attribute instead
        loc.select_option(value=value, timeout=ACTION_TIMEOUT_MS)


def _do_upload(scope, action) -> str | None:
    path = resolve_upload_path(action.get("value") or "")
    if not path.is_file():
        return f"file-not-found: {path}"
    loc = scope.locator(action["selector"]).first
    loc.set_input_files(str(path), timeout=ACTION_TIMEOUT_MS)
    return None


def execute_action(page, action: dict) -> dict:
    """Run one payload action. Always returns
    {selector, label, action, ok, error} — never raises."""
    act = action.get("action") or ""
    res = {"selector": action.get("selector", ""),
           "label": action.get("label", ""),
           "action": act, "ok": False, "error": None}
    if act == "skip":
        res["ok"] = True
        return res
    try:
        scope = _scope(page, action.get("frame_path"))
        if act == "fill":
            _do_fill(scope, page, action)
        elif act == "select_option":
            if action.get("kind") == "radio":
                res["error"] = _do_radio(scope, action)
            else:
                _do_select(scope, action)
        elif act == "check":
            scope.locator(action["selector"]).first.check(timeout=ACTION_TIMEOUT_MS)
        elif act == "upload":
            res["error"] = _do_upload(scope, action)
        else:
            res["error"] = f"unknown-action: {act}"
        res["ok"] = res["error"] is None
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return res


def execute_actions(page, actions: list[dict]) -> dict:
    """Run a whole payload action list; failures never abort the batch.

    Returns {"results": [per-action dicts], "ok": n, "failed": n} — the
    caller (apply_session) decides whether the failure rate or a specific
    failure blocks submission."""
    results = [execute_action(page, a) for a in actions]
    failed = sum(1 for r in results if not r["ok"])
    return {"results": results, "ok": len(results) - failed, "failed": failed}
