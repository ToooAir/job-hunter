#!/usr/bin/env python3
"""One-time repair of jobs.apply_url for bundesagentur rows.

BA answers its externeURL/allianzpartnerUrl field with *something* for every
posting, but two rows in three it is not an apply channel — a scheme-less bare
host ("www.zalando.de", which does not even open as a link) or a self-reference
to arbeitsagentur.de. The ingest path now gates that field (phase1
_ba_apply_url); this fixes the rows that predate the gate.

Idempotent: it re-derives the verdict from the CURRENT apply_url, so a row
already pointing at its own BA page (or at a genuinely usable link) is left
alone and re-runs are no-ops. Links a later ats_scan resolved to something real
are usable and therefore preserved.

Two passes: the jobs row, then the DRAFT application snapshots, which carry
their own copy of the link (that copy is what the review card renders, so
repairing only the jobs table would leave the reviewer clicking the same dead
link). Submitted snapshots are never touched — they record where the user
actually applied.

Run inside the container (WAL-safe; never open the bind-mounted DB from the
macOS host):

    docker exec job-hunter-apply_api-1 python3 scripts/backfill_ba_apply_url.py --dry-run
    docker exec job-hunter-apply_api-1 python3 scripts/backfill_ba_apply_url.py
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase1_ingestor import _ba_apply_url  # noqa: E402


def backfill(db_path: str, dry_run: bool = False, limit: int | None = None) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # the scheduler's scorer may be writing concurrently — wait for the WAL
    # write lock instead of erroring out with "database is locked"
    conn.execute("PRAGMA busy_timeout = 30000")

    sql = "SELECT id, url, apply_url, status FROM jobs WHERE source = 'bundesagentur'"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    print(f"scanning {len(rows)} bundesagentur rows (dry_run={dry_run})...")

    changed = 0
    by_status: Counter = Counter()
    for r in rows:
        fixed = _ba_apply_url(r["apply_url"] or "", r["url"])
        if fixed == (r["apply_url"] or ""):
            continue
        changed += 1
        by_status[r["status"]] += 1
        if changed <= 10:
            print(f"  {r['apply_url']!r} -> {fixed}")
        if not dry_run:
            conn.execute("UPDATE jobs SET apply_url = ? WHERE id = ?", (fixed, r["id"]))
            if changed % 2000 == 0:
                conn.commit()
                print(f"  ...{changed} repaired")
    if not dry_run:
        conn.commit()
    verb = "would repair" if dry_run else "repaired"
    print(f"done: {verb} {changed} of {len(rows)} rows")
    if by_status:
        print("  by status:", dict(by_status.most_common()))

    # Pass 2: the draft cards render the SNAPSHOT's copy of the link.
    snaps = conn.execute(
        "SELECT s.id, s.apply_url, j.url, j.apply_url AS job_apply "
        "FROM application_snapshots s JOIN jobs j ON j.id = s.job_id "
        "WHERE s.status = 'draft' AND j.source = 'bundesagentur'"
    ).fetchall()
    fixed = 0
    for sn in snaps:
        want = _ba_apply_url(sn["apply_url"] or "", sn["job_apply"] or sn["url"])
        if want == (sn["apply_url"] or ""):
            continue
        fixed += 1
        if fixed <= 5:
            print(f"  draft {sn['id']}: {sn['apply_url']!r} -> {want}")
        if not dry_run:
            conn.execute("UPDATE application_snapshots SET apply_url = ? WHERE id = ?",
                         (want, sn["id"]))
    if not dry_run:
        conn.commit()
    print(f"done: {verb} {fixed} of {len(snaps)} draft snapshots")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--db", default=os.getenv("DB_PATH", "./data/jobs.db"))
    args = ap.parse_args()
    backfill(args.db, dry_run=args.dry_run, limit=args.limit)
