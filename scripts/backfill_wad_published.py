#!/usr/bin/env python3
"""One-time: age the wearedevelopers rows ingested on 2026-09-02/03 from their
own Published date.

The rewritten scraper (Markdown endpoints) ingested 1,866 aggregated postings
in one night with expires_at = fetched + 45d. A 2026-09-03 sample of 144
listings showed a third were already older than 45 days on arrival — dead on
the downstream board, but about to cost a scoring call each and then a draft.
The scraper now gates on Published at ingest; this script applies the same
rule to the rows that predate it, reading Published from each posting's .md.

Idempotent (only rows with the new URL shape and no rule marker in notes are
touched), resumable, and read-only towards the site (one GET per row).

    docker exec -i -w /app job-hunter-pipeline-1 python3 - < scripts/backfill_wad_published.py --dry-run
    docker exec -w /app job-hunter-pipeline-1 python3 scripts/backfill_wad_published.py
"""

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase1_ingestor import _wad_md, _wad_published_date, WAD_MAX_AGE_DAYS  # noqa: E402

_PUBLISHED_RE = re.compile(r"^- \*\*Published:\*\*\s*(.+?)\s*$", re.M)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--since", default="2026-09-02T20:00:00")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    rows = conn.execute(
        "SELECT id, url, title, company, status FROM jobs WHERE source = 'wearedevelopers' "
        "  AND url LIKE '%/jobs/ext/%-%' AND fetched_at >= ? "
        "  AND status IN ('un-scored', 'scored') ORDER BY fetched_at", (args.since,)
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows)} rows (dry_run={args.dry_run})", flush=True)
    today = datetime.now(timezone.utc).date()
    aged = expired = gone = unparsed = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        md = _wad_md(r["url"] + ".md")
        if md is None:
            gone += 1  # 404/5xx — ats_scan's .md liveness will settle it
            continue
        m = _PUBLISHED_RE.search(md)
        published = _wad_published_date(m.group(1)) if m else None
        if not published:
            unparsed += 1
            continue
        expires = (datetime.combine(published, datetime.min.time(), tzinfo=timezone.utc)
                   + timedelta(days=WAD_MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        stale = (today - published).days > WAD_MAX_AGE_DAYS
        if stale:
            expired += 1
        else:
            aged += 1
        if not args.dry_run:
            if stale:
                conn.execute("UPDATE jobs SET expires_at = ?, status = 'expired' WHERE id = ?",
                             (expires, r["id"]))
            else:
                conn.execute("UPDATE jobs SET expires_at = ? WHERE id = ?", (expires, r["id"]))
            # commit per row: each iteration waits ~1.2s on the network, and
            # an open write transaction across 25 of them held the DB lock
            # for 30s — long enough to fail init_db (5s busy_timeout) in
            # the dashboard and resume_stats (observed 2026-09-03)
            conn.commit()
        if i % 100 == 0:
            print(f"  {i}/{len(rows)} aged={aged} expired={expired} gone={gone} "
                  f"unparsed={unparsed} {time.time() - t0:.0f}s", flush=True)
    conn.commit()
    conn.close()
    print(f"done: aged={aged} expired(stale>{WAD_MAX_AGE_DAYS}d)={expired} gone={gone} "
          f"unparsed={unparsed} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
