#!/usr/bin/env python3
"""agentic_probe.py — offline A/B: deterministic mapper vs agentic mapper.

Loads a captured field table (tools/agentic_capture.py output) and runs BOTH
mapping strategies against it with the real profile + real Mistral, then prints
a side-by-side report and scores the agentic run against the decision gate.

No browser, no submit — pure offline measurement of "can the LLM drive the page
from ID-tagged text". Runs in the pipeline container (LLM stack + profile).

    docker exec job-hunter-pipeline-1 python tools/agentic_probe.py \
        --capture data/agentic/charles_capture.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.profile_loader import load_profile  # noqa: E402

# Decision gate (agreed with user before build).
GATE = {"grounding_min": 0.90, "max_hallucinated": 0, "no_never_fill": True}


def run_current(fields, profile, job_meta) -> dict:
    """Reproduce the pipeline's Pass B mapping chain (minus the reused
    cover-letter draft, which isn't in a capture): deterministic + LLM map +
    open-question answers."""
    from utils.apply_llm import answer_open_questions, map_pending_fields
    from utils.field_mapper import map_fields

    det = map_fields(fields, profile)
    llm = map_pending_fields(det["pending"], profile, job_meta)
    ans = answer_open_questions(llm.get("open_questions") or [], job_meta, kb_context="")
    actions = det["actions"] + llm["actions"] + ans["actions"]
    return {"actions": actions,
            "cover_letter_slots": llm.get("cover_letter_slots") or []}


def grounding_accuracy(diag: dict) -> float:
    rows = diag.get("llm_rows") or 0
    if rows == 0:
        return 0.0
    good = rows - len(diag.get("hallucinated_ids", [])) - len(diag.get("kind_mismatch", []))
    return good / rows


def _act_str(a: dict | None) -> str:
    if a is None:
        return "·"
    v = (a.get("value") or "").replace("\n", " ")
    if len(v) > 38:
        v = v[:35] + "…"
    return f'{a["action"]}={v}' if v else a["action"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, help="*_capture.json from agentic_capture")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cap = json.loads(Path(args.capture).read_text())
    fields = cap["fields"]
    job_meta = cap["job"]
    profile = load_profile()

    print(f"=== {job_meta.get('company')} — {job_meta.get('title')} ===")
    print(f"capture: {args.capture}  |  fields: {len(fields)}  "
          f"captcha: {cap.get('captcha')}\n")

    from utils.agentic_mapper import map_page_agentic
    cur = run_current(fields, profile, job_meta)
    ag = map_page_agentic(fields, profile, job_meta)
    diag = ag["diagnostics"]

    cur_by_sel = {a["selector"]: a for a in cur["actions"]}
    ag_by_sel = {a["selector"]: a for a in ag["actions"]}

    # ── side-by-side per field ──────────────────────────────────────────
    print(f"{'#':>2} {'kind':9} {'label':34} | {'CURRENT':24} | AGENTIC")
    print("-" * 100)
    for i, f in enumerate(fields):
        sel = f.get("selector", "")
        label = (f.get("label") or "")[:34]
        print(f"{i:>2} {f.get('kind',''):9} {label:34} | "
              f"{_act_str(cur_by_sel.get(sel)):24} | {_act_str(ag_by_sel.get(sel))}")

    # ── scores ──────────────────────────────────────────────────────────
    total = len(fields)
    cur_n, ag_n = len(cur["actions"]), len(ag["actions"])
    ga = grounding_accuracy(diag)
    print("\n── scores ─────────────────────────────────────────────")
    print(f"coverage      current {cur_n}/{total}   agentic {ag_n}/{total}")
    print(f"LLM rows      {diag['llm_rows']}  (addressed {diag['addressed']}/{total})")
    print(f"grounding     {ga:.0%}  "
          f"(hallucinated {len(diag['hallucinated_ids'])}, "
          f"kind-mismatch {len(diag['kind_mismatch'])})")
    print(f"hallucinated  ids={diag['hallucinated_ids']}")
    print(f"rejected      placeholder={len(diag['placeholder_rejected'])}  "
          f"option-not-grounded={len(diag['option_not_grounded'])}  "
          f"never-fill={len(diag['never_fill_blocked'])}")

    # ── decision gate ───────────────────────────────────────────────────
    # Gate rationale: on well-built forms the deterministic mapper already
    # fills everything fillable, so parity (not strictly-greater) is the right
    # bar — agentic must not REGRESS and must never hallucinate. A strict
    # ">current" gate would punish agentic for the deterministic mapper being
    # good, which is backwards.
    checks = {
        f"grounding ≥ {GATE['grounding_min']:.0%}": ga >= GATE["grounding_min"],
        f"hallucinated == {GATE['max_hallucinated']}":
            len(diag["hallucinated_ids"]) <= GATE["max_hallucinated"],
        "coverage ≥ current (no regression)": ag_n >= cur_n,
        "no never-fill leak": len(diag["never_fill_blocked"]) == 0
            or all(b not in ag_by_sel for b in diag["never_fill_blocked"]),
    }
    print("\n── decision gate ──────────────────────────────────────")
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    verdict = "PASS — agentic worth pursuing" if all(checks.values()) else \
              "FAIL — see failed checks"
    print(f"\n  VERDICT: {verdict}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "job": job_meta, "total": total,
            "current_actions": cur["actions"], "agentic_actions": ag["actions"],
            "agentic_unfilled": ag["unfilled"], "diagnostics": diag,
            "scores": {"coverage_current": cur_n, "coverage_agentic": ag_n,
                       "grounding": ga}, "gate": checks,
        }, ensure_ascii=False, indent=1))
        print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    main()
