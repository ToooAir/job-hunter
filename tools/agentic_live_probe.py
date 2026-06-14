#!/usr/bin/env python3
"""agentic_live_probe.py — execute an agentic draft on the LIVE form (prepare-only).

Everything before this was offline (capture -> map -> score). This is the first
time an agentic mapping is actually TYPED INTO a live page, to answer the one
thing the offline probes structurally cannot:

  1. do agentic's actions LAND on the live DOM (right control, value sticks)?
  2. does a custom widget (react-select & friends) actually get SELECTED, not
     merely clicked? — watchlist #5, online-only.

One headless session: goto_apply_page -> extract_form_tree -> map_page_agentic
-> form_executor.execute_actions (FILL ONLY) -> read each control's live value
back -> screenshot. There is NO submit: the executor has no submit action and
this tool never clicks one. Outputs land in a gitignored dir (real PII stays
local). Hit a real form ONCE per run — this types into a live company form.

    docker exec job-hunter-pipeline-1 python tools/agentic_live_probe.py \
        --job-id <id> [--name mistral2] [--out-dir data/agentic]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_queue import DEFAULT_DB_PATH  # noqa: E402
from utils.db import init_db  # noqa: E402
from utils.form_executor import (  # noqa: E402
    _RADIO_LABEL_JS,
    ACTION_TIMEOUT_MS,
    _norm,
    _scope,
    execute_actions,
)

# Visible text of the smallest plausible widget container — a react-select
# selection shows up as text here, not as an <input> value, which is exactly
# the thing we cannot see offline.
_WIDGET_TEXT_JS = """
el => {
  const box = el.closest(
    '[class*="select"],[class*="Select"],[role="combobox"],[class*="dropdown"]'
  ) || el.parentElement || el;
  return (box.innerText || box.textContent || '').trim();
}
"""

_SELECTED_OPTION_JS = (
    "el => el.options && el.options[el.selectedIndex] "
    "? el.options[el.selectedIndex].text : ''"
)


def _read_live_value(page, action: dict) -> str:
    """Read what the control actually shows AFTER the fill — the proof of landing."""
    scope = _scope(page, action.get("frame_path"))
    sel = action.get("selector", "")
    kind = action.get("kind")
    act = action.get("action")
    loc = scope.locator(sel).first
    try:
        if act == "check":
            return "checked" if loc.is_checked(timeout=ACTION_TIMEOUT_MS) else "unchecked"
        if act == "select_option" and kind == "radio":
            name = loc.get_attribute("name", timeout=ACTION_TIMEOUT_MS)
            if name:
                checked = scope.locator(
                    f'input[type="radio"][name="{name}"]:checked').first
                if checked.count():
                    return (checked.evaluate(_RADIO_LABEL_JS) or "").strip()
            return "<none checked>"
        if kind == "custom":
            return loc.evaluate(_WIDGET_TEXT_JS)
        if act == "select_option":
            return loc.evaluate(_SELECTED_OPTION_JS)
        if act == "upload":
            n = loc.evaluate("el => (el.files && el.files.length) || 0")
            return f"<{n} file(s) attached>"
        return loc.input_value(timeout=ACTION_TIMEOUT_MS)
    except Exception as exc:  # reading is best-effort; never abort the probe
        return f"<read-error: {type(exc).__name__}>"


def _landed(action: dict, live: str) -> bool:
    """Did the intended value actually take? Lenient containment for widgets
    (their visible text wraps the choice), exact-ish for plain inputs."""
    act = action.get("action")
    if act == "check":
        return live == "checked"
    if act == "upload":
        return live.startswith("<") and "0 file" not in live
    intended, got = _norm(action.get("value")), _norm(live)
    if not intended:
        return False
    if action.get("kind") == "custom" or act == "select_option":
        return intended in got
    return got == intended or (len(intended) >= 3 and intended in got)


def main() -> None:
    ap = argparse.ArgumentParser(description="Execute an agentic draft on a live form (no submit).")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--out-dir", default="data/agentic")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    conn = init_db(args.db)
    row = conn.execute(
        "SELECT id, company, title, source, COALESCE(apply_url, url) AS target, url "
        "FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
    conn.close()
    if row is None:
        sys.exit(f"job {args.job_id} not found")
    job = dict(row)
    name = args.name or job["id"]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from utils.agentic_mapper import map_page_agentic
    from utils.browser import extract_form_tree, goto_apply_page, headless_session
    from utils.profile_loader import load_profile

    profile = load_profile()
    print(f"LIVE probe: {job['company']} — {job['title']}\n  target: {job['target']}",
          flush=True)

    shot_path = out_dir / f"{name}_live.png"
    with headless_session() as context:
        page = context.new_page()
        report = goto_apply_page(page, job["target"], title=job.get("title"))
        active = report.pop("page", page)
        if not report["form_found"]:
            sys.exit(f"no form found at {active.url} — nothing to execute")

        tree = extract_form_tree(active)
        fields = [f.to_dict() for f in tree["fields"]]
        print(f"  fields: {len(fields)}  captcha: {report['captcha']}", flush=True)

        mapped = map_page_agentic(fields, profile, job)
        actions = mapped["actions"]
        print(f"  agentic actions: {len(actions)}  (executing, FILL ONLY)\n", flush=True)

        exec_report = execute_actions(active, actions)

        # read back what each executed control now shows — the landing proof
        landings = []
        for a, r in zip(actions, exec_report["results"]):
            live = _read_live_value(active, a) if r["ok"] else "<exec-failed>"
            landings.append({
                "label": a.get("label", ""), "kind": a.get("kind"),
                "action": a.get("action"), "intended": a.get("value", ""),
                "exec_ok": r["ok"], "exec_error": r["error"],
                "live_value": live, "landed": r["ok"] and _landed(a, live),
            })

        active.screenshot(path=str(shot_path), full_page=True)
        final_url = active.url

    # ── report ──────────────────────────────────────────────────────────────
    customs = [x for x in landings if x["kind"] == "custom"]
    landed_n = sum(1 for x in landings if x["landed"])
    print(f"{'label':32} {'action':14} {'intended':22} | {'live value':26} land")
    print("-" * 104)
    for x in landings:
        flag = "✅" if x["landed"] else ("⚠️" if x["exec_ok"] else "❌")
        star = " «custom»" if x["kind"] == "custom" else ""
        print(f"{(x['label'] or '')[:32]:32} {x['action']:14} "
              f"{str(x['intended'])[:22]:22} | {str(x['live_value'])[:26]:26} {flag}{star}")

    print("\n── landing summary ───────────────────────────────────")
    print(f"executed     {exec_report['ok']} ok / {exec_report['failed']} failed")
    print(f"landed       {landed_n}/{len(landings)}")
    if customs:
        ok = sum(1 for x in customs if x["landed"])
        print(f"CUSTOM WIDGET {ok}/{len(customs)} selected  ← the online-only question")
        for x in customs:
            print(f"   • {x['label'][:40]!r}: intended {x['intended']!r} → "
                  f"shows {x['live_value']!r}  [{'SELECTED' if x['landed'] else 'NOT SELECTED'}]")
    else:
        print("CUSTOM WIDGET (none on this form)")
    print(f"\nscreenshot   {shot_path}  ← eyeball the custom widget here")
    print(f"final_url    {final_url}")

    (out_dir / f"{name}_live.json").write_text(json.dumps({
        "job": job, "final_url": final_url, "n_fields": len(fields),
        "exec": {"ok": exec_report["ok"], "failed": exec_report["failed"]},
        "landings": landings, "screenshot": str(shot_path),
    }, ensure_ascii=False, indent=1))
    print(f"written      {out_dir}/{name}_live.json")


if __name__ == "__main__":
    main()
