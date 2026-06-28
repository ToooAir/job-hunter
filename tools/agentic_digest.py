#!/usr/bin/env python3
"""agentic_digest.py — disagreement analysis across many captured forms.

Target-3 experiment: instead of hand-picking malformed forms, run BOTH mappers
over a batch of captures and surface every field where they DISAGREE. Those
disagreements are exactly where "is agentic smarter?" gets decided — a human
judges each. Agreements (both fill the same / both skip) are only counted.

Runs in the pipeline container (LLM stack + profile). One Mistral call/form.

    docker exec job-hunter-pipeline-1 python tools/agentic_digest.py \
        data/agentic/*_capture.json
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from utils.agentic_mapper import map_page_agentic  # noqa: E402
from utils.profile_loader import load_profile  # noqa: E402


def run_current(fields, profile, job_meta) -> list[dict]:
    from utils.apply_llm import answer_open_questions, map_pending_fields
    from utils.field_mapper import map_fields
    det = map_fields(fields, profile)
    llm = map_pending_fields(det["pending"], profile, job_meta)
    ans = answer_open_questions(llm.get("open_questions") or [], job_meta, kb_context="")
    return det["actions"] + llm["actions"] + ans["actions"]


def _norm(a: dict) -> tuple:
    return (a.get("action"), " ".join((a.get("value") or "").split()).casefold())


def _short(a: dict | None) -> str:
    if a is None:
        return "—skip—"
    v = " ".join((a.get("value") or "").split())
    if len(v) > 44:
        v = v[:41] + "…"
    return f'{a["action"]}={v}' if v else a["action"]


def main() -> None:
    patterns = sys.argv[1:] or ["data/agentic/*_capture.json"]
    paths: list[str] = []
    for p in patterns:
        paths.extend(sorted(glob.glob(p)))
    profile = load_profile()

    agg = {"only_agentic": 0, "only_current": 0, "value_diff": 0,
           "agree_fill": 0, "both_skip": 0, "hallucinated": 0, "kind_mismatch": 0}

    for path in paths:
        cap = json.loads(Path(path).read_text())
        fields, job = cap["fields"], cap["job"]
        if not fields:
            continue
        cur = {a["selector"]: a for a in run_current(fields, profile, job)}
        ag_out = map_page_agentic(fields, profile, job)
        ag = {a["selector"]: a for a in ag_out["actions"]}
        diag = ag_out["diagnostics"]
        agg["hallucinated"] += len(diag["hallucinated_ids"])
        agg["kind_mismatch"] += len(diag["kind_mismatch"])

        rows = []
        for i, f in enumerate(fields):
            sel = f.get("selector", "")
            c, a = cur.get(sel), ag.get(sel)
            if c is None and a is None:
                agg["both_skip"] += 1
            elif c is not None and a is None:
                agg["only_current"] += 1
                rows.append(("ONLY-CURRENT", i, f, c, a))
            elif c is None and a is not None:
                agg["only_agentic"] += 1
                rows.append(("ONLY-AGENTIC", i, f, c, a))
            elif _norm(c) == _norm(a):
                agg["agree_fill"] += 1
            else:
                agg["value_diff"] += 1
                rows.append(("VALUE-DIFF", i, f, c, a))

        company = (job.get("company") or "?")[:24]
        print(f"\n===== {company} — {Path(path).stem} ({len(fields)} fields, "
              f"{len(rows)} disagreements) =====")
        for tag, i, f, c, a in rows:
            label = (f.get("label") or "")[:40]
            print(f"  {tag:13} [{i:2}] {f.get('kind',''):8} {label:40}")
            print(f"                 current: {_short(c)}")
            print(f"                 agentic: {_short(a)}")

    print("\n############ AGGREGATE ############")
    for k in ("agree_fill", "both_skip", "only_agentic", "only_current",
              "value_diff", "hallucinated", "kind_mismatch"):
        print(f"  {k:14} {agg[k]}")
    disagreements = agg["only_agentic"] + agg["only_current"] + agg["value_diff"]
    decided = agg["agree_fill"] + agg["both_skip"] + disagreements
    print(f"  {'disagreements':14} {disagreements} / {decided} fields "
          f"({disagreements / decided:.0%})")


if __name__ == "__main__":
    main()
