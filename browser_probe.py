#!/usr/bin/env python3
"""browser_probe.py — measure the generic-form pool with the real browser layer.

For every alive queue-eligible job whose apply form lives on a company
website (ats unknown / unknown-external / js-page), drive the Step 3
headless backend end to end: navigate, answer the cookie wall, follow the
apply button, extract the field node table. The per-job verdicts decide
where Step 4's effort goes.

Verdicts:
  ok           form reached, fields extracted
  shadow-only  controls exist but only inside shadow DOM (vision/manual)
  captcha      form reached but captcha present (Tier 3)
  no-form      page loads but no application form found
  nav-error    navigation failed (timeout, DNS, TLS, ...)

Output: data/browser_probe.json + summary table on stdout. Read-only on DB.

Usage:
    python browser_probe.py [--limit N] [--source heise]
"""

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils.browser import extract_form_tree, goto_apply_page, headless_session  # noqa: E402

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "jobs.db"
OUT_JSON = ROOT / "data" / "browser_probe.json"

GENERIC_ATS = ("unknown", "unknown-external", "js-page")

GERMANY_LIKE = [
    "German", "Deutschland", "Hamburg", "Berlin", "Munich", "München",
    "Köln", "Cologne", "Frankfurt", "Stuttgart", "Düsseldorf", "Leipzig",
    "Bremen", "Hannover",
]


def fetch_targets(limit=None, source=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    loc_clause = " OR ".join(f"location LIKE '%{kw}%'" for kw in GERMANY_LIKE)
    ats_clause = ",".join(f"'{a}'" for a in GENERIC_ATS)
    sql = (
        "SELECT id, source, company, title, url, apply_url, ats, fit_grade, match_score "
        "FROM jobs WHERE status='scored' "
        "AND (fit_grade='A' OR (fit_grade='B' AND match_score >= 70)) "
        f"AND ats IN ({ats_clause}) AND ({loc_clause}) "
    )
    if source:
        sql += f"AND source = '{source}' "
    sql += "ORDER BY source, match_score DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Boards we never auto-submit on (account walls / bot-hostile). A WAD or
# board job whose apply link lands here is a Tier 3 answer-sheet case.
EXTERNAL_BOARDS = ("xing.com", "indeed.com", "linkedin.com", "stepstone.de")


def verdict_of(report, tree):
    if report["error"]:
        return "nav-error"
    host = (report.get("final_url") or "").lower()
    if any(b in host for b in EXTERNAL_BOARDS):
        return "external-board"
    if report["captcha"]:
        return "captcha"
    if tree and tree["fields"]:
        return "ok"
    if report["controls"]["shadow"] > 0 and report["controls"]["light"] == 0:
        return "shadow-only"
    return "no-form"


def probe_one(context, job):
    page = context.new_page()
    try:
        # the resolved apply_url (when scan found one) beats the board URL
        target = job["apply_url"] or job["url"]
        if target.startswith("mailto:"):
            return {"verdict": "email-only", "report": {"url": target}, "fields": []}
        report = goto_apply_page(page, target)
        active = report.pop("page", page)  # apply button may open a new tab
        tree = None
        if report["form_found"]:
            tree = extract_form_tree(active)
        if active is not page:
            active.close()
        return {
            "verdict": verdict_of(report, tree),
            "report": report,
            "n_fields": len(tree["fields"]) if tree else 0,
            "frames": tree["frames"] if tree else [],
            "shadow_controls": tree["shadow_controls"] if tree else report["controls"]["shadow"],
            "pruned_bytes": sum(len(h.encode()) for h in tree["pruned"].values()) if tree else 0,
            "has_file_field": any(f.kind == "file" for f in tree["fields"]) if tree else False,
            "fields": [
                {"kind": f.kind, "label": f.label, "required": f.required}
                for f in (tree["fields"] if tree else [])
            ][:40],
        }
    finally:
        page.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--source", default=None)
    args = parser.parse_args()

    targets = fetch_targets(args.limit, args.source)
    print(f"探測 {len(targets)} 筆自建/unknown 職缺(headless persistent profile)...", flush=True)

    results = []
    started = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with headless_session() as context:
        for i, job in enumerate(targets, 1):
            t0 = time.monotonic()
            try:
                probe = probe_one(context, job)
            except Exception as exc:  # never let one site kill the batch
                probe = {"verdict": "probe-crash", "report": {"error": str(exc)[:200]}, "fields": []}
            took = time.monotonic() - t0
            results.append({**{k: job[k] for k in (
                "id", "source", "company", "title", "url", "ats", "fit_grade", "match_score")},
                **probe, "took_s": round(took, 1)})
            r = probe.get("report", {})
            print(
                f"  [{i}/{len(targets)}] {probe['verdict']:<12} "
                f"fields={probe.get('n_fields', 0):<3} "
                f"cookie={'Y' if r.get('cookie_clicked') else '-'} "
                f"apply={'Y' if r.get('clicked_apply') else '-'} "
                f"{job['company'][:28]} ({job['source']}) {took:.0f}s",
                flush=True,
            )
            time.sleep(random.uniform(1.0, 2.5))

    OUT_JSON.write_text(json.dumps(
        {"started": started, "results": results}, ensure_ascii=False, indent=1))

    from collections import Counter
    counts = Counter(r["verdict"] for r in results)
    print(f"\n=== 探測結果(共 {len(results)} 筆)===")
    for v, n in counts.most_common():
        print(f"  {v:<12} {n:>3}  ({n / len(results):.0%})")
    ok = [r for r in results if r["verdict"] == "ok"]
    if ok:
        sizes = sorted(r["pruned_bytes"] for r in ok)
        fields = sorted(r["n_fields"] for r in ok)
        print(f"\nok 樣本:fields 中位數 {fields[len(fields)//2]}、"
              f"pruned 中位數 {sizes[len(sizes)//2]} bytes、"
              f"含檔案上傳 {sum(1 for r in ok if r['has_file_field'])}/{len(ok)}、"
              f"用 iframe {sum(1 for r in ok if r['frames'] and r['frames'] != ['(main)'])}/{len(ok)}")
    print(f"\n明細已寫入 {OUT_JSON}")


if __name__ == "__main__":
    main()
