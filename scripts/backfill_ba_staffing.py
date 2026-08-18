#!/usr/bin/env python3
"""One-time backfill of jobs.staffing for bundesagentur rows.

The column is populated going forward at ingest, from BA's own
istArbeitnehmerUeberlassung / istPrivateArbeitsvermittlung flags. Existing rows
predate it, and rows are never re-upserted, so without this the flag half of the
detection is dead for them — utils.staffing still catches whatever the company
NAME gives away, but not the ones that give nothing away (plusYou, hyrUP, AERO
HighProfessionals in a 45-row sample).

Costs one detail call per row, so it defaults to the queue-eligible pool (the
only rows the flag can currently change anything for). --all covers the corpus.
Idempotent: only rows where staffing IS NULL are touched, so it is safe to
re-run and safe to stop/resume. A posting BA has since withdrawn (404) is left
NULL rather than guessed at.

    docker exec job-hunter-apply_api-1 python3 scripts/backfill_ba_staffing.py --dry-run
    docker exec job-hunter-apply_api-1 python3 scripts/backfill_ba_staffing.py
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from utils.ba_api import BA_HEADERS, ba_detail_url, ba_refnr_from_url  # noqa: E402
from utils.staffing import is_staffing  # noqa: E402

_ELIGIBLE = ("fit_grade = 'A' OR (fit_grade = 'B' AND match_score >= 70)")


def backfill(db_path: str, dry_run: bool = False, do_all: bool = False,
             limit: int | None = None) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    sql = ("SELECT id, company, url FROM jobs "
           "WHERE source = 'bundesagentur' AND staffing IS NULL")
    if not do_all:
        sql += f" AND status = 'scored' AND ({_ELIGIBLE})"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    print(f"scanning {len(rows)} rows (all={do_all}, dry_run={dry_run})...")

    flagged = written = gone = failed = 0
    for i, r in enumerate(rows, 1):
        refnr = ba_refnr_from_url(r["url"])
        if not refnr:
            failed += 1
            continue
        try:
            resp = requests.get(ba_detail_url(refnr), headers=BA_HEADERS, timeout=15)
        except requests.RequestException:
            failed += 1
            continue
        if resp.status_code in (404, 410):
            gone += 1          # withdrawn: leave NULL rather than guess
            continue
        if resp.status_code != 200:
            failed += 1
            continue
        try:
            d = resp.json() or {}
        except ValueError:
            failed += 1        # HTML maintenance page
            continue
        value = int(is_staffing(
            r["company"],
            d.get("istArbeitnehmerUeberlassung") or d.get("istPrivateArbeitsvermittlung")))
        flagged += value
        written += 1
        if not dry_run:
            conn.execute("UPDATE jobs SET staffing = ? WHERE id = ?", (value, r["id"]))
            if written % 200 == 0:
                conn.commit()
        time.sleep(0.4)        # same politeness delay the scraper uses
        if i % 50 == 0:
            print(f"  ...{i}/{len(rows)}")
    if not dry_run:
        conn.commit()
    conn.close()
    verb = "would write" if dry_run else "wrote"
    print(f"done: {verb} {written} rows ({flagged} are staffing), "
          f"{gone} withdrawn (left NULL), {failed} failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", dest="do_all", action="store_true",
                    help="whole corpus, not just the queue-eligible pool")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--db", default=os.getenv("DB_PATH", "./data/jobs.db"))
    args = ap.parse_args()
    backfill(args.db, dry_run=args.dry_run, do_all=args.do_all, limit=args.limit)
