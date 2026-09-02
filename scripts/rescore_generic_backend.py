#!/usr/bin/env python3
"""One-time targeted rescore after the 2026-09-02 grading-rules re-anchor.

The old anchors made "no AI focus" a 50–69 partial match, so generic
Python/Node backend roles landed in grade C (50–59) and never reached the
queue — 80 of 209 such rows in the German pool cite the AI gap in their
top_3_reasons. The new rules score a plain backend role in the core stack as
70–84. Re-scoring the whole pool would cost thousands of LLM calls for nothing;
only the rows that could flip are worth it:

    status='scored' AND fit_grade='C' AND match_score 50–59
    AND jd_language_req != 'de_required'      (language, not stack, is the wall)
    AND German location (queue GERMANY_KEYWORDS) AND not a student title
    AND the JD mentions python / node / django / fastapi / typescript

Runs the normal scorer path (pre-flight, source bonus, seniority penalty), so
the result is exactly what a fresh ingest would have produced. A row the LLM
still scores below 60 simply stays C. Not idempotent in the sense that every
run spends LLM calls; it is safe to re-run (a row is just scored again).

    docker exec job-hunter-pipeline-1 python3 scripts/rescore_generic_backend.py --dry-run
    docker exec job-hunter-pipeline-1 python3 scripts/rescore_generic_backend.py
"""

import argparse
import logging
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_queue import GERMANY_KEYWORDS, title_excluded  # noqa: E402

DB_PATH = "data/jobs.db"
STACK_RE = re.compile(r"python|node\.?js|django|fastapi|typescript", re.I)


def _in_germany(location: str | None) -> bool:
    low = (location or "").lower()
    return any(kw.lower() in low for kw in GERMANY_KEYWORDS)


def select_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT id, title, company, location, match_score, jd_language_req, source, "
        "       raw_jd_text FROM jobs "
        "WHERE status = 'scored' AND fit_grade = 'C' "
        "  AND match_score BETWEEN 50 AND 59 "
        "  AND jd_language_req != 'de_required'"
    ).fetchall()
    return [
        r for r in rows
        if _in_germany(r["location"])
        and not title_excluded(r["title"])
        and STACK_RE.search(r["raw_jd_text"] or "")
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    rows = select_rows(conn)
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows)} candidate rows (dry_run={args.dry_run})")
    for r in rows:
        print(f"  {r['match_score']} {r['jd_language_req']:12s} {r['source']:15s} "
              f"{(r['company'] or '')[:28]:28s} | {(r['title'] or '')[:50]}")
    if args.dry_run or not rows:
        conn.close()
        return

    ids = [r["id"] for r in rows]
    before = {r["id"]: r["match_score"] for r in rows}
    conn.close()

    from phase2_scorer import score_jobs  # noqa: E402  (LLM stack import)
    from utils.db import init_db, reset_to_unscored  # noqa: E402

    c = init_db(args.db)
    reset_to_unscored(c, ids)
    c.close()
    score_jobs(db_path=args.db, job_ids=ids)

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    after = c.execute(
        f"SELECT id, title, company, fit_grade, match_score, status FROM jobs "
        f"WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    c.close()
    grades = {"A": 0, "B": 0, "C": 0}
    other = 0
    print("\n=== result ===")
    for r in after:
        if r["status"] != "scored":
            other += 1
            print(f"  {r['status']:10s} {(r['company'] or '')[:28]:28s} | {(r['title'] or '')[:50]}")
            continue
        grades[r["fit_grade"]] = grades.get(r["fit_grade"], 0) + 1
        if r["fit_grade"] != "C":
            print(f"  {before[r['id']]} -> {r['fit_grade']}{r['match_score']:3d} "
                  f"{(r['company'] or '')[:28]:28s} | {(r['title'] or '')[:50]}")
    print(f"\nA={grades['A']} B={grades['B']} C={grades['C']} not-scored={other} of {len(ids)}")


if __name__ == "__main__":
    main()
