#!/usr/bin/env python3
"""One-time backfill of jobs.company_aliases from each row's raw JD.

The column is populated going forward at ingest (utils.db.upsert_job); this
fills it for rows that predate the feature. Idempotent — only touches rows
where company_aliases IS NULL, so it is safe to re-run and safe to stop/resume.

Run inside the container (WAL-safe; never open the bind-mounted DB from the
macOS host):

    docker exec job-hunter-apply_api-1 python3 scripts/backfill_company_aliases.py --active-only
    docker exec job-hunter-apply_api-1 python3 scripts/backfill_company_aliases.py
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.company_alias import extract_company_aliases  # noqa: E402

_ACTIVE = ("applied", "interview_1", "interview_2")


def backfill(db_path: str, active_only: bool = False, limit: int | None = None) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # the scheduler's scorer may be writing concurrently — wait for the WAL
    # write lock instead of erroring out with "database is locked"
    conn.execute("PRAGMA busy_timeout = 30000")
    where = "company_aliases IS NULL"
    params: list = []
    if active_only:
        where += " AND status IN (%s)" % ",".join("?" for _ in _ACTIVE)
        params += list(_ACTIVE)
    sql = f"SELECT id, company, raw_jd_text FROM jobs WHERE {where}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    print(f"scanning {len(rows)} rows (active_only={active_only})...")
    updated = with_alias = 0
    for r in rows:
        aliases = extract_company_aliases(r["company"] or "", r["raw_jd_text"] or "")
        conn.execute("UPDATE jobs SET company_aliases = ? WHERE id = ?",
                     (aliases, r["id"]))
        updated += 1
        if aliases:
            with_alias += 1
        if updated % 2000 == 0:
            conn.commit()
            print(f"  ...{updated} updated ({with_alias} with an alias)")
    conn.commit()
    conn.close()
    print(f"done: {updated} rows updated, {with_alias} carry at least one alias")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--active-only", action="store_true",
                    help="only backfill applied / interview rows (fast sanity pass)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--db", default=os.getenv("DB_PATH", "./data/jobs.db"))
    args = ap.parse_args()
    backfill(args.db, active_only=args.active_only, limit=args.limit)
