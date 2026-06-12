#!/usr/bin/env python3
"""apply_stage1.py — Stage 1 draft generation over the daily apply queue.

Two passes (Step 4 design, approved 2026-06-12):
  Pass A (browser): for each in-budget queue job, reach the apply form with
  the Step 3 headless backend, extract the field node table, close the page.
  heise jobs follow the "Originalanzeige" link out to the original posting
  (user decision: never use heise's own application wizard). Every visited
  job gets its ats_checked_at refreshed (JIT liveness).

  Pass B (LLM, no browser): run the per-job LangGraph pipeline — map fields,
  generate/reuse content, verify, assign tier, save a draft snapshot.

Accounting invariant: every queue job ends as exactly one of
  draft saved (tier 1/2/3) | failed (reason recorded). Nothing is dropped.

Usage:
    python apply_stage1.py [--limit N] [--budget N] [--source X]
                           [--dry-run] [--db PATH] [--out PATH]
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))

from utils.apply_graph import build_graph  # noqa: E402
from utils.apply_queue import DEFAULT_DB_PATH, build_queue  # noqa: E402
from utils.db import init_db  # noqa: E402
from utils.profile_loader import load_profile  # noqa: E402

OUT_JSON = Path(__file__).parent / "data" / "stage1_run.json"

# Boards we never auto-submit on; landing there means Tier 3 answer sheet.
EXTERNAL_BOARDS = ("xing.com", "indeed.com", "linkedin.com", "stepstone.de")


def _has_apply_signature(fields) -> bool:
    """An application form has an upload, a free-text answer, or several
    text entries including a plain one (name etc.). One lonely text box is
    a search bar (Workato lesson); email-only means a newsletter/job-alert
    widget (Riverty lesson)."""
    kinds = [f.kind for f in fields]
    textish = sum(1 for k in kinds
                  if k in ("text", "email", "tel", "url", "number", "date"))
    return ("file" in kinds or "textarea" in kinds
            or (textish >= 2 and "text" in kinds))


def verdict_of(report: dict, tree: dict | None) -> str:
    if report["error"]:
        return "nav-error"
    host = (report.get("final_url") or "").lower()
    if any(b in host for b in EXTERNAL_BOARDS):
        return "external-board"
    if report["captcha"]:
        return "captcha"
    if tree and tree["fields"]:
        return "ok" if _has_apply_signature(tree["fields"]) else "weak-form"
    if report["controls"].get("password"):
        return "account-wall"
    if report["controls"]["shadow"] > 0 and report["controls"]["light"] == 0:
        return "shadow-only"
    return "no-form"


def _heise_original(page, url: str) -> str | None:
    """heise detail page → the 'Originalanzeige' link to the original site."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        link = page.locator("a", has_text=re.compile("Originalanzeige", re.I)).first
        href = link.get_attribute("href", timeout=3_000)
        return urljoin(page.url, href) if href else None
    except Exception:
        return None


def run_pass_a(jobs: list[dict]) -> list[dict]:
    """Browser sweep: per-job verdict + field node table. Web-read-only."""
    from utils.browser import extract_form_tree, goto_apply_page, headless_session

    states = []
    with headless_session() as context:
        for i, job in enumerate(jobs, 1):
            page = context.new_page()
            t0 = time.monotonic()
            try:
                target = job.get("apply_url") or job["url"]
                if target.startswith("mailto:"):
                    states.append({"job": job, "verdict": "email-only",
                                   "fields": [], "apply_url": target})
                    continue
                if job.get("source") == "heise":
                    target = _heise_original(page, job["url"]) or target
                report = goto_apply_page(page, target)
                active = report.pop("page", page)
                tree = extract_form_tree(active) if report["form_found"] else None
                if active is not page:
                    active.close()
                note = (f"pass-a: cookie={'Y' if report.get('cookie_clicked') else '-'}"
                        f" apply={'Y' if report.get('clicked_apply') else '-'}")
                if report.get("error"):
                    note += f" error={str(report['error'])[:80]}"
                states.append({
                    "job": job,
                    "verdict": verdict_of(report, tree),
                    "fields": [f.to_dict() for f in tree["fields"]] if tree else [],
                    "pruned": tree["pruned"] if tree else {},
                    "apply_url": report.get("final_url") or target,
                    "notes": [note],
                })
            except Exception as exc:  # one bad site never kills the sweep
                states.append({"job": job, "verdict": "probe-crash", "fields": [],
                               "notes": [f"pass-a crash: {str(exc)[:150]}"]})
            finally:
                page.close()
            s = states[-1]
            print(f"  [A {i}/{len(jobs)}] {s['verdict']:<14} fields={len(s['fields']):<3}"
                  f" {job['company'][:30]} ({time.monotonic() - t0:.0f}s)", flush=True)
            time.sleep(random.uniform(1.0, 2.5))
    return states


def refresh_liveness(conn, states: list[dict]) -> None:
    """Visiting the page IS the liveness check — record when we did."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for s in states:
        conn.execute(
            "UPDATE jobs SET ats_checked_at = ?, "
            "apply_url = COALESCE(?, apply_url) WHERE id = ?",
            (now, s.get("apply_url"), s["job"]["id"]),
        )
    conn.commit()


def enrich_jobs(conn, queue: list[dict]) -> list[dict]:
    """Queue rows + the columns Pass B needs (JD text, reused cover letter)."""
    out = []
    for job in queue:
        row = conn.execute(
            "SELECT COALESCE(translated_jd_text, raw_jd_text) AS description, "
            "       cover_letter_draft FROM jobs WHERE id = ?",
            (job["id"],),
        ).fetchone()
        out.append({**job, **(dict(row) if row else {})})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1: generate draft applications.")
    parser.add_argument("--limit", type=int, default=None, help="cap jobs this run")
    parser.add_argument("--budget", type=int, default=None)
    parser.add_argument("--source", default=None, help="only this source (debug)")
    parser.add_argument("--dry-run", action="store_true",
                        help="no DB writes (snapshots, liveness)")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--out", default=str(OUT_JSON))
    args = parser.parse_args()

    profile = load_profile()  # strict: refuses TODO residue / missing CV

    conn = init_db(args.db)
    queue = build_queue(conn, budget=args.budget)["queue"]
    if args.source:
        queue = [j for j in queue if j["source"] == args.source]
    if args.limit:
        queue = queue[:args.limit]
    jobs = enrich_jobs(conn, queue)
    print(f"Stage 1:佇列取 {len(jobs)} 筆(dry-run={args.dry_run})", flush=True)

    states = run_pass_a(jobs)
    if not args.dry_run:
        refresh_liveness(conn, states)
    conn.close()

    from utils.apply_llm import CALL_STATS
    graph = build_graph()
    config = {"configurable": {
        "profile": profile,
        "db_path": args.db,
        "dry_run": args.dry_run,
        "qdrant_path": str(Path(__file__).parent / "qdrant_data"),
    }}

    results, failures = [], []
    for i, state in enumerate(states, 1):
        job = state["job"]
        try:
            final = graph.invoke(state, config=config)
            results.append(final)
            vr = final.get("verifier_report")
            print(f"  [B {i}/{len(states)}] tier={final.get('tier')} "
                  f"actions={len(final.get('actions') or [])} "
                  f"qa={len(final.get('custom_qa') or [])} "
                  f"verify={'-' if vr is None else ('pass' if vr.get('pass') else 'flagged')} "
                  f"{job['company'][:30]}", flush=True)
        except Exception as exc:
            failures.append({"job": job, "error": str(exc)[:200]})
            print(f"  [B {i}/{len(states)}] FAILED {job['company'][:30]}: "
                  f"{str(exc)[:120]}", flush=True)

    # ── accounting ──────────────────────────────────────────────────────
    by_tier: dict = {}
    for r in results:
        by_tier[r.get("tier")] = by_tier.get(r.get("tier"), 0) + 1
    det = sum(1 for r in results for a in (r.get("actions") or [])
              if str(a.get("source", "")).startswith("profile:"))
    llm = sum(1 for r in results for a in (r.get("actions") or [])
              if a.get("source") in ("llm", "cover_letter"))
    print(f"\n=== Stage 1 對帳(佇列 {len(jobs)} = 草稿 {len(results)} "
          f"+ 失敗 {len(failures)})===")
    for tier in sorted(by_tier):
        print(f"  Tier {tier}: {by_tier[tier]}")
    print(f"  填值 actions:確定性 {det} / LLM+CL {llm};LLM 呼叫 {CALL_STATS['calls']} 次")
    if failures:
        for f in failures:
            print(f"  FAILED {f['job']['company']}: {f['error']}")

    out_path = Path(args.out)
    out_path.write_text(json.dumps({
        "started": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": args.dry_run,
        "llm_calls": CALL_STATS["calls"],
        "results": [{
            **{k: r["job"].get(k) for k in
               ("id", "source", "company", "title", "ats", "dedup", "rank")},
            "verdict": r.get("verdict"),
            "tier": r.get("tier"),
            "apply_url": r.get("apply_url"),
            "snapshot_id": r.get("snapshot_id"),
            "n_actions": len(r.get("actions") or []),
            "n_unfilled": len(r.get("unfilled") or []),
            "actions": r.get("actions") or [],
            "unfilled": r.get("unfilled") or [],
            "custom_qa": r.get("custom_qa") or [],
            "verifier_report": r.get("verifier_report") or {},
            "notes": r.get("notes") or [],
        } for r in results],
        "failures": [{"id": f["job"]["id"], "company": f["job"]["company"],
                      "error": f["error"]} for f in failures],
    }, ensure_ascii=False, indent=1))
    print(f"\n明細已寫入 {out_path}")


if __name__ == "__main__":
    main()
