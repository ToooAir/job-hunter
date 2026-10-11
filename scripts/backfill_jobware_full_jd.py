#!/usr/bin/env python3
"""One-time backfill of jobware raw_jd_text: the full advert, not the Aufgaben half.

Until 2026-10-11 the scraper stored job["task"] from the listing API, which is
literally the tasks — that payload carries no requirements field at all. Every
knock-out criterion (language requirement, core programming language,
Arbeitnehmerüberlassung notice) lives only in the iframe at
resourceUrlWithTracking. Scoring half an advert does not produce a low score,
it produces a high one: nothing is left in the text to fail on. 531 jobware
rows outscored the same company+title held in full from another source, 211 of
those twins having been zeroed outright by the German-language gate.

The iframe URL is not a stored column, and jobware bounces anonymous views of
/job/<slug> to /account/login, so a row cannot be repaired from what the DB
holds. The only route back to it is the listing API, which answers with live
postings only — so this reaches the rows still on the board and no others. That
is also the right scope: a withdrawn posting is not worth an LLM re-score.

Repaired rows are reset to 'un-scored' so the next phase2 run re-grades them
against the real advert. jd_hash is recomputed (it keys cross-source dedup off
chars 50-550, which the new text changes) and translated_jd_text is dropped,
being a translation of the half that is going away.

Idempotent: a row is only written when the iframe is longer than what is
stored, so re-running is safe, and so is stopping and resuming. Rows already
applied to are never touched — the application is sent, and re-scoring them
would only muddy the funnel.

    docker exec job-hunter-apply_api-1 python3 scripts/backfill_jobware_full_jd.py --dry-run
    docker exec job-hunter-apply_api-1 python3 scripts/backfill_jobware_full_jd.py
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from phase1_ingestor import (  # noqa: E402
    JW_JOB_BASE, _jw_iframe_jd, _jw_safe_get,
)
from utils.db import _jd_hash  # noqa: E402

# Only rows a repair can still change anything for.
_REPAIRABLE = ("source = 'jobware' AND applied_at IS NULL "
               "AND status IN ('scored', 'un-scored')")

_PACE = 1.0   # the scraper's own delay between listing calls


def _live_iframes(cfg: dict, pace: float) -> dict[str, str]:
    """{job_url: iframe_url} for every posting the listing matrix returns now.

    Walks the same keyword x location matrix as the daily run, at the same
    pace — this is one run's worth of listing traffic, not a crawl.
    """
    locations = list(cfg.get("locations") or [])
    if cfg.get("include_remote", True):
        locations.append("")          # empty = all Germany incl. remote
    keywords = cfg["keywords"]
    found: dict[str, str] = {}
    total = len(locations) * len(keywords)
    done = 0
    for wo in locations:
        for kw in keywords:
            params: dict = {"jw_jobname": kw, "jw_internal": "true"}
            if wo:
                params["jw_jobort"] = wo
                params["jw_radius"] = str(cfg.get("radius_km", 50))
            for job in _jw_safe_get(params):
                slug = job.get("url", "")
                iframe = job.get("resourceUrlWithTracking", "")
                if slug and iframe:
                    found[f"{JW_JOB_BASE}/{slug}"] = iframe
            done += 1
            if done % 20 == 0:
                print(f"  listing {done}/{total} queries, {len(found)} live postings seen")
            time.sleep(pace)
    return found


def backfill(db_path: str, dry_run: bool = False, limit: int | None = None,
             pace: float = _PACE) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    # Newest first: only postings still on the board can be repaired at all, and
    # an unordered LIMIT samples the oldest rows, which are all long gone.
    sql = (f"SELECT id, company, title, url, raw_jd_text FROM jobs "
           f"WHERE {_REPAIRABLE} ORDER BY fetched_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    print(f"{len(rows)} repairable rows (dry_run={dry_run})")

    with open(Path(__file__).resolve().parents[1] / "config/search_targets.yaml") as fh:
        cfg = yaml.safe_load(fh)["jobware"]
    print("walking the listing matrix for iframe URLs...")
    live = _live_iframes(cfg, pace)
    print(f"{len(live)} live postings on the board")

    matched = written = no_longer_listed = not_longer = failed = 0
    for r in rows:
        iframe_url = live.get(r["url"])
        if not iframe_url:
            no_longer_listed += 1
            continue
        matched += 1
        full = _jw_iframe_jd(iframe_url)
        time.sleep(pace)
        if not full:
            failed += 1
            continue
        old = r["raw_jd_text"] or ""
        if len(full) <= len(old):
            not_longer += 1       # already full, or the iframe gave us less
            continue
        written += 1
        print(f"  +{len(full) - len(old):5d}B  {r['company'][:32]:32s} {r['title'][:38]}")
        if not dry_run:
            conn.execute(
                """UPDATE jobs SET
                     raw_jd_text = ?, jd_hash = ?, translated_jd_text = NULL,
                     status = 'un-scored', scored_at = NULL, match_score = NULL,
                     fit_grade = NULL, top_3_reasons = NULL, cover_letter_draft = NULL,
                     jd_language_req = NULL, visa_restriction = NULL,
                     salary_range = NULL, contract_type = NULL
                   WHERE id = ?""",
                (full, _jd_hash(full), r["id"]),
            )
            if written % 50 == 0:
                conn.commit()
    if not dry_run:
        conn.commit()
    conn.close()
    verb = "would rewrite" if dry_run else "rewrote"
    print(f"\ndone: {verb} {written} rows; {matched} still listed, "
          f"{no_longer_listed} gone from the board (left as-is), "
          f"{not_longer} already complete, {failed} iframe fetch failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pace", type=float, default=_PACE)
    ap.add_argument("--db", default=os.getenv("DB_PATH", "./data/jobs.db"))
    args = ap.parse_args()
    backfill(args.db, dry_run=args.dry_run, limit=args.limit, pace=args.pace)
