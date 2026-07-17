#!/usr/bin/env python3
"""apply_stage1.py — Stage 1 draft generation over the daily apply queue.

Two passes (Step 4 design, approved 2026-06-12):
  Pass A (browser): for each in-budget queue job, reach the apply form with
  the Step 3 headless backend, extract the field node table, close the page.
  heise jobs follow the "Originalanzeige" link out to the original posting
  (user decision: never use heise's own application wizard). Every visited
  job gets its ats_checked_at refreshed (JIT liveness).

  Pass B (LLM, no browser): run the per-job LangGraph pipeline — reuse the
  scored cover letter, verify, assign tier, save a draft snapshot. (The
  field-mapping chain was retired 2026-07-02 — facts are filled live at
  apply time by the extension's /fill-plan; the extracted field table now
  only feeds the weak-form verdict.)

Accounting invariant: every queue job ends as exactly one of
  draft saved (tier 1/2/3) | failed (reason recorded) | expired (posting
  gone) | skipped (un-appliable form). Nothing is dropped, and the final
  tally names all four.

Usage:
    python apply_stage1.py [--limit N] [--budget N] [--source X]
                           [--dry-run] [--db PATH] [--out PATH]
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).parent))

from utils.apply_graph import build_graph  # noqa: E402
from utils.apply_queue import (  # noqa: E402
    DEFAULT_DB_PATH, build_queue, is_addressable, topup_budget,
)
from utils.db import init_db  # noqa: E402
from utils.profile_loader import load_profile  # noqa: E402

# 40 until 2026-07-17; with weak-form eating ~half of a pull, 55 keeps the
# post-gate draft wall near the ~25-35 the reviewer works through in a day.
TARGET_INVENTORY = int(os.getenv("APPLY_TARGET_INVENTORY", "55"))

OUT_JSON = Path(__file__).parent / "data" / "stage1_run.json"

# Boards we never auto-submit on; landing there means Tier 3 answer sheet.
# germantechjobs.de hosts its own quick-apply form, but it redirects
# unpredictably and its field markup drifts (milia lesson) —
# treat it like the other boards: Tier 3 answer sheet, human submits.
EXTERNAL_BOARDS = ("xing.com", "indeed.com", "linkedin.com", "stepstone.de",
                   "germantechjobs.de")


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
    if report.get("gone_signal"):
        return "gone"
    if report["controls"].get("password"):
        return "account-wall"
    if report["controls"]["shadow"] > 0 and report["controls"]["light"] == 0:
        return "shadow-only"
    return "no-form"


def is_unappliable(verdict: str, job: dict) -> bool:
    """Verdicts that historically never convert into an application — the
    2026-07-08 abandoned-draft review: 7/7 heise-own-form and 6/6
    non-addressable weak-form drafts died in the reviewer's hands. Such jobs
    get no draft and leave the queue (status='skipped') instead of occupying
    the review wall. Addressable weak-forms survive: the probe under-extracts
    structured ATS disguised behind an iframe (the Workato gh_jid lesson)."""
    if verdict == "heise-own-form":
        return True
    return verdict == "weak-form" and not is_addressable(job)


# Manual lane (2026-07-17): with supply now the constraint, up to this many
# A-grade weak-forms per run survive the gate as Tier 3 hand-apply drafts
# (scoring-stage CL + facts, human finds the real apply channel). B-grade
# weak-forms still skip — the lane trades review effort for A-value only.
WEAK_FORM_A_CAP = int(os.getenv("APPLY_WEAK_FORM_A_CAP", "5"))


def skip_unappliable(conn, states: list[dict], dry_run: bool) -> list[dict]:
    """Split off un-appliable states; mark their jobs skipped. Returns the
    states that continue to Pass B."""
    keep, skipped = [], []
    lane = 0
    for s in states:
        if is_unappliable(s["verdict"], s["job"]):
            if (s["verdict"] == "weak-form" and s["job"].get("fit_grade") == "A"
                    and lane < WEAK_FORM_A_CAP):
                lane += 1
                s.setdefault("notes", []).append("manual lane: A-grade weak-form")
                print(f"  ➜ 手投巷道（A 級 weak-form {lane}/{WEAK_FORM_A_CAP}，"
                      f"{s['job']['company'][:30]}）→ 保留為 Tier 3 草稿", flush=True)
                keep.append(s)
            else:
                skipped.append(s)
        else:
            keep.append(s)
    if skipped and not dry_run:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        for s in skipped:
            conn.execute(
                "UPDATE jobs SET status = 'skipped', "
                "notes = COALESCE(notes || char(10), '') || ? WHERE id = ?",
                (f"[{now}] stage1: un-appliable form ({s['verdict']}) — "
                 "no draft generated; set status='scored' to resurrect",
                 s["job"]["id"]),
            )
        conn.commit()
    for s in skipped:
        print(f"  ✗ 表單不可投（{s['verdict']}，{s['job']['company'][:30]}）"
              f"→ 標 skipped，不生成草稿", flush=True)
    return keep


def _first_link_href(page, pattern: str, timeout_ms: int) -> str | None:
    """href of the first anchor whose text matches, or None (never raises)."""
    try:
        link = page.locator("a", has_text=re.compile(pattern, re.I)).first
        link.wait_for(state="attached", timeout=timeout_ms)
        return link.get_attribute("href", timeout=3_000)
    except Exception:
        return None


def _heise_original(page, url: str) -> str | None:
    """heise detail page → the EXTERNAL posting's URL, or None.

    heise ships three apply shapes (user field report + live probe 2026-07-08):
      1. 'Jetzt bewerben' href leaves heise directly        → return it
      2. it opens jobs.heise.de/application whose FIRST page carries the
         'Originalanzeige' link (read via one GET, never filled) → return that
      3. that page has no Originalanzeige: heise-hosted wizard → None

    The Originalanzeige link used to sit on the detail page itself; heise
    moved it into the application page (probe: 0/8 detail pages still had
    it), which silently turned every heise job into 'heise-own-form'. The
    detail-page lookup stays as a cheap first try for the legacy layout.
    Returns None when only heise's own wizard remains — we never use it
    (user decision). Fail closed: a missed external link costs one manual
    application, whereas filling heise's wizard submits on a forbidden channel.
    """
    from utils.browser import _settle, dismiss_cookie_banner
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        dismiss_cookie_banner(page)
        _settle(page)
    except Exception:
        return None
    href = _first_link_href(page, "Originalanzeige", 3_000)  # legacy layout
    if not href:
        bew = _first_link_href(page, r"jetzt\s+bewerben", 8_000)
        if not bew:
            return None
        dest = urljoin(page.url, bew)
        if urlparse(dest).netloc.lower().endswith("heise.de"):
            # shape 2/3: read (never fill) the wizard's first page — that is
            # where heise now shows the Originalanzeige when one exists
            try:
                page.goto(dest, wait_until="domcontentloaded", timeout=20_000)
                _settle(page)
            except Exception:
                return None
            href = _first_link_href(page, "Originalanzeige", 8_000)
            if not href:
                return None  # shape 3: heise-hosted wizard only
        else:
            href = dest  # shape 1: the apply button itself leaves heise
    dest = urljoin(page.url, href)
    # never accept a link that loops back into heise's own application wizard
    if "jobs.heise.de/application" in dest.lower():
        return None
    return dest


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
                    original = _heise_original(page, job["url"])
                    if not original:
                        # heise-hosted (useCompanyForm) or link unreachable:
                        # never fill heise's own wizard (user decision). Fail
                        # closed → Tier 3 answer sheet, human applies manually.
                        states.append({
                            "job": job, "verdict": "heise-own-form",
                            "fields": [], "apply_url": None,
                            "notes": ["pass-a: no external Originalanzeige; "
                                      "skipped heise's own application form"],
                        })
                        continue
                    target = original
                report = goto_apply_page(page, target, title=job.get("title"))
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
                    # final_url is only trustworthy as an apply link when the
                    # PRUNED tree has fields — form_found counts raw controls,
                    # and a homepage's search boxes pass that bar (Zenjob
                    # lesson). Otherwise the probe may have drifted and
                    # persisting the URL would poison the next run's target.
                    "apply_url": (report.get("final_url")
                                  if tree and tree.get("fields") else None),
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
    parser.add_argument("--budget", type=int, default=None,
                        help="explicit generation count (overrides inventory top-up)")
    parser.add_argument("--target", type=int, default=None,
                        help=f"target live-draft inventory (default {TARGET_INVENTORY})")
    parser.add_argument("--no-sweep", action="store_true",
                        help="skip the pending-draft liveness sweep")
    parser.add_argument("--source", default=None, help="only this source (debug)")
    parser.add_argument("--job-ids", default=None,
                        help="comma-separated job ids — regenerate just these")
    parser.add_argument("--dry-run", action="store_true",
                        help="no DB writes (snapshots, liveness)")
    parser.add_argument("--no-agentic", action="store_true",
                        help="kill-switch: skip the agentic long-tail fallback")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--out", default=str(OUT_JSON))
    args = parser.parse_args()

    profile = load_profile()  # strict: refuses TODO residue / missing CV

    conn = init_db(args.db)

    # Liveness sweep first: prune dead drafts (frees inventory slots) and flag
    # the suspicious, so the top-up count reflects what's actually still live.
    if not args.no_sweep and not args.job_ids:
        from utils.draft_liveness import sweep_drafts
        t = sweep_drafts(conn, dry_run=args.dry_run)
        print(f"掃視:checked {t['checked']} → live {t['live']} / "
              f"dead {t['dead']}(撤回+expired) / suspect {t['suspicious']}(標記)", flush=True)

    # Inventory model: top the live-draft pool back up to the target. --budget is
    # an explicit override; --job-ids regenerates specific jobs (no truncation).
    target = args.target if args.target is not None else TARGET_INVENTORY
    if args.job_ids:
        gen_budget = 10_000
    elif args.budget is not None:
        gen_budget = args.budget
    else:
        live = conn.execute(
            "SELECT COUNT(*) FROM application_snapshots WHERE status='draft'").fetchone()[0]
        gen_budget = topup_budget(live, target)
        print(f"庫存:現有 {live} 活草稿,目標 {target} → 本輪生成上限 {gen_budget}", flush=True)

    queue = build_queue(conn, budget=gen_budget, include_stale=True)["queue"]
    if args.source:
        queue = [j for j in queue if j["source"] == args.source]
    if args.job_ids:
        wanted = {x.strip() for x in args.job_ids.split(",") if x.strip()}
        queue = [j for j in queue if j["id"] in wanted]
        missing = wanted - {j["id"] for j in queue}
        if missing:
            print(f"⚠ 不在佇列中(可能 in-flight/不合格):{', '.join(sorted(missing))}")
    if args.limit:
        queue = queue[:args.limit]
    jobs = enrich_jobs(conn, queue)
    print(f"Stage 1:佇列取 {len(jobs)} 筆(dry-run={args.dry_run})", flush=True)

    states = run_pass_a(jobs)
    if not args.dry_run:
        refresh_liveness(conn, states)
    gone = [s for s in states if s["verdict"] == "gone"]
    if gone:
        # a vanished posting gets no draft — expire it instead of wasting
        # LLM calls and review time on a Tier 3 shell (Zenjob lesson)
        if not args.dry_run:
            from utils.db import mark_expired
            mark_expired(conn, [s["job"]["id"] for s in gone])
        for s in gone:
            print(f"  ✗ 職缺已下架（{s['job']['company'][:30]}）→ 標 expired，"
                  f"不生成草稿", flush=True)
        states = [s for s in states if s["verdict"] != "gone"]
    n_gone = len(gone)
    n_before_gate = len(states)
    states = skip_unappliable(conn, states, dry_run=args.dry_run)
    n_unappliable = n_before_gate - len(states)
    # Guardian dial: 7-day abandon-reason tally, printed with the final
    # accounting. A bucket suddenly getting fat = run a /guardian pass.
    from utils.snapshot_io import abandon_tally
    tally = abandon_tally(conn)
    tally_line = ", ".join(f"{k} {v}" for k, v in tally.most_common()) or "none"
    conn.close()

    from utils.apply_llm import CALL_STATS
    graph = build_graph()
    config = {"configurable": {
        "profile": profile,
        "db_path": args.db,
        "dry_run": args.dry_run,
        "qdrant_path": str(Path(__file__).parent / "qdrant_data"),
        "enable_agentic_fallback": not args.no_agentic,
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
          f"+ 失敗 {len(failures)} + 下架 {n_gone} + 不可投 {n_unappliable})===")
    for tier in sorted(by_tier):
        print(f"  Tier {tier}: {by_tier[tier]}")
    print(f"  填值 actions:確定性 {det} / LLM+CL {llm};LLM 呼叫 {CALL_STATS['calls']} 次")
    print(f"  放棄近7天:{tally_line}")
    if failures:
        for f in failures:
            print(f"  FAILED {f['job']['company']}: {f['error']}")

    out_path = Path(args.out)
    out_path.write_text(json.dumps({
        "started": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": args.dry_run,
        "llm_calls": CALL_STATS["calls"],
        "expired_gone": n_gone,
        "skipped_unappliable": n_unappliable,
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
