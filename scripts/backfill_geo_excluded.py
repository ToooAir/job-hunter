#!/usr/bin/env python3
"""One-time repair of rows the geo gate scored before it knew their country.

2026-09-05: the veto list in utils/geo_de knew "Brazil" and "India" but not
bare "US", "United Arab Emirates", "Chile", the German-language country names
bundesagentur emits ("SPANIEN"), or bare foreign city names ("Los Angeles",
"Remote / Ottawa"). 516 rows were LLM-scored anyway and then sat in the apply
queue's supply as jobs no German employer can hire into — 121 of them A or B.
The gate is fixed at ingest/scoring time; this moves the already-scored rows
out of the supply.

status 'scored' → 'skipped', the same exit apply_stage1.skip_unappliable uses
for un-appliable jobs, with a notes line saying how to resurrect one. Rows are
NOT re-scored (the LLM verdict is irrelevant — they can never be applied to)
and rows in a pipeline status (applied / interview / rejected / …) are never
touched: those record what the user actually did.

Idempotent: it re-derives the verdict from the CURRENT location, so a re-run
finds nothing left to do.

Run inside the container (WAL-safe; never open the bind-mounted DB from the
macOS host):

    docker exec job-hunter-pipeline-1 python3 scripts/backfill_geo_excluded.py --dry-run
    docker exec job-hunter-pipeline-1 python3 scripts/backfill_geo_excluded.py
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.geo_de import has_non_de_marker  # noqa: E402

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")


def select_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Scored rows whose location the fixed gate now vetoes."""
    rows = conn.execute(
        "SELECT id, company, title, location, url, fit_grade, match_score "
        "FROM jobs WHERE status = 'scored'"
    ).fetchall()
    return [r for r in rows if has_non_de_marker(r["location"], r["url"])]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    rows = select_rows(conn)
    if args.limit:
        rows = rows[: args.limit]

    by_loc = Counter((r["location"] or "").strip() for r in rows)
    by_grade = Counter(r["fit_grade"] for r in rows)
    print(f"{len(rows)} scored rows now vetoed by the geo gate "
          f"(dry_run={args.dry_run})")
    print(f"  by grade: {dict(sorted(by_grade.items(), key=lambda kv: str(kv[0])))}")
    print("  top locations:")
    for loc, n in by_loc.most_common(25):
        print(f"    {n:5d}  {loc!r}")

    if args.dry_run or not rows:
        conn.close()
        return

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for r in rows:
        conn.execute(
            "UPDATE jobs SET status = 'skipped', "
            "notes = COALESCE(notes || char(10), '') || ? WHERE id = ?",
            (f"[{now}] geo backfill: location {(r['location'] or '')[:60]!r} is "
             "outside Germany — left the apply queue; set status='scored' to resurrect",
             r["id"]),
        )
    conn.commit()
    print(f"\nmarked {len(rows)} rows status='skipped'")

    left = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'scored' AND location = 'US'"
    ).fetchone()[0]
    print(f"remaining scored rows with location='US': {left}")
    conn.close()


if __name__ == "__main__":
    main()
