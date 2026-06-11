"""apply_queue.py — build the daily semi-auto apply queue (Step 2).

Pure Python over the jobs/application_snapshots tables; no LLM, no network.

Eligibility (all must hold):
  * status = 'scored'  (pipeline/skipped/expired/error are therefore out)
  * located in Germany
  * fit_grade A, or B with match_score >= 70
  * no in-flight snapshot for the job itself (draft/approved/submitted)
  * liveness: ats_checked_at <= LIVENESS_MAX_AGE_DAYS and ats not in DEAD_ATS;
    stale/unchecked candidates land in `needs_recheck` — they re-enter after a
    JIT re-verify (ats_scan), they are NOT silently dropped

Ranking (deterministic, applied in order):
  1. freshness bucket — fetched_at <= FRESH_BUCKET_DAYS first (protects
     perishable sources like wearedevelopers, whose jobs die young)
  2. grade A before B
  3. match_score descending
  4. fetched_at newest first

Dedup gate (per candidate, on normalized company name):
  block — company already has a pipeline record, or another job of the same
          company has an in-flight snapshot
  warn  — same jd_hash already applied under another job (recruiter repost),
          or second job of the same company within this batch

Budget: env APPLY_DAILY_BUDGET (default 25) caps the queue; the rest is
reported as over_budget.

Dry run (zero writes):
    python -m utils.apply_queue [--top 30] [--budget N] [--db PATH]
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import IN_FLIGHT_SNAPSHOT_STATUSES, init_db  # noqa: E402

DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "jobs.db")
DEFAULT_BUDGET = 25
LIVENESS_MAX_AGE_DAYS = 7
FRESH_BUCKET_DAYS = 3
DEAD_ATS = ("gone", "fetch-error")

# Same notion of "in the pipeline" as the dashboard: once a company has any of
# these, a new application there needs a human decision first.
PIPELINE_STATUSES = ("applied", "interview_1", "interview_2", "offer", "rejected", "ghosted")

# Keep in sync with ats_scan.GERMANY_LIKE (duplicated to avoid importing
# requests/bs4 here).
GERMANY_KEYWORDS = [
    "German", "Deutschland", "Hamburg", "Berlin", "Munich", "München",
    "Köln", "Cologne", "Frankfurt", "Stuttgart", "Düsseldorf", "Leipzig",
    "Bremen", "Hannover",
]

# Trailing legal suffixes stripped before comparing company names.
# "GmbH & Co. KG" must be tried before its parts; UG often appears as
# "UG (haftungsbeschränkt)".
_LEGAL_SUFFIX_RE = re.compile(
    r"\s*(?:"
    r"gmbh\s*&\s*co\.?\s*kg|se\s*&\s*co\.?\s*kg|&\s*co\.?\s*kg|co\.?\s*kg"
    r"|gmbh|ag|se|kg|inc\.?|ltd\.?|llc|ug(?:\s*\(haftungsbeschränkt\))?"
    r")\s*$",
    re.IGNORECASE,
)


def normalize_company(name: str) -> str:
    """Lowercased company name with legal suffixes stripped, for dedup matching."""
    norm = re.sub(r"\s+", " ", (name or "").strip().lower())
    while True:
        stripped = _LEGAL_SUFFIX_RE.sub("", norm).rstrip(" ,.-")
        if stripped == norm or not stripped:
            break
        norm = stripped
    return norm


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)  # all DB timestamps treated as local-naive
    except ValueError:
        return None


def job_age_days(fetched_at: str | None, now: datetime) -> int | None:
    dt = _parse_dt(fetched_at)
    return (now - dt).days if dt else None


def sort_key(job: dict, now: datetime):
    age = job_age_days(job["fetched_at"], now)
    fresh_bucket = 0 if age is not None and age <= FRESH_BUCKET_DAYS else 1
    grade_rank = 0 if job["fit_grade"] == "A" else 1
    fetched = _parse_dt(job["fetched_at"]) or datetime.min
    return (fresh_bucket, grade_rank, -(job["match_score"] or 0), -fetched.timestamp())


class DedupContext:
    """Company/jd_hash state the gate checks against; built once per queue run."""

    def __init__(self, pipeline_companies, in_flight, applied_jd_hashes):
        self.pipeline_companies = pipeline_companies      # norm company -> status
        self.in_flight = in_flight                        # norm company -> job_id
        self.applied_jd_hashes = applied_jd_hashes        # jd_hash -> job_id
        self.batch_companies: dict[str, str] = {}         # norm company -> job_id (this batch)

    @classmethod
    def from_db(cls, conn):
        placeholders = ",".join("?" for _ in PIPELINE_STATUSES)
        pipeline = {
            normalize_company(r["company"]): r["status"]
            for r in conn.execute(
                f"SELECT company, status FROM jobs WHERE status IN ({placeholders})",
                PIPELINE_STATUSES,
            )
        }
        snap_placeholders = ",".join("?" for _ in IN_FLIGHT_SNAPSHOT_STATUSES)
        in_flight = {
            normalize_company(r["company"]): r["job_id"]
            for r in conn.execute(
                f"""SELECT s.job_id, j.company FROM application_snapshots s
                    JOIN jobs j ON j.id = s.job_id
                    WHERE s.status IN ({snap_placeholders})""",
                IN_FLIGHT_SNAPSHOT_STATUSES,
            )
        }
        applied_hashes = {
            r["jd_hash"]: r["id"]
            for r in conn.execute(
                f"""SELECT id, jd_hash FROM jobs
                    WHERE status IN ({placeholders})
                      AND jd_hash IS NOT NULL AND jd_hash != ''""",
                PIPELINE_STATUSES,
            )
        }
        return cls(pipeline, in_flight, applied_hashes)


def dedup_gate(job: dict, ctx: DedupContext) -> tuple[str, str]:
    """Return (verdict, reason): verdict is 'block' | 'warn' | 'ok'.

    ok/warn candidates are registered as this batch's entry for their company,
    so a later job of the same company in the same batch gets a warn.
    """
    company = normalize_company(job["company"])
    if company in ctx.pipeline_companies:
        return "block", f"company in pipeline ({ctx.pipeline_companies[company]})"
    if company in ctx.in_flight:
        return "block", f"company has in-flight snapshot (job {ctx.in_flight[company]})"

    verdict, reason = "ok", ""
    if job.get("jd_hash") and job["jd_hash"] in ctx.applied_jd_hashes:
        verdict, reason = "warn", f"same JD applied as job {ctx.applied_jd_hashes[job['jd_hash']]}"
    elif company in ctx.batch_companies:
        verdict, reason = "warn", f"2nd job of company in batch (job {ctx.batch_companies[company]})"
    ctx.batch_companies.setdefault(company, job["id"])
    return verdict, reason


def fetch_candidates(conn) -> list[dict]:
    """Grade/score/location/status-eligible jobs, before liveness and dedup."""
    loc_clause = " OR ".join(f"location LIKE '%{kw}%'" for kw in GERMANY_KEYWORDS)
    rows = conn.execute(
        "SELECT id, source, company, title, url, location, fit_grade, match_score, "
        "       fetched_at, jd_hash, ats, apply_url, ats_checked_at "
        "FROM jobs "
        "WHERE status = 'scored' "
        "  AND (fit_grade = 'A' OR (fit_grade = 'B' AND match_score >= 70)) "
        f" AND ({loc_clause})"
    )
    return [dict(r) for r in rows]


def build_queue(conn, budget: int | None = None, now: datetime | None = None) -> dict:
    """Build the ranked apply queue. Read-only: never writes any status.

    Returns {'queue', 'over_budget', 'blocked', 'needs_recheck', 'dead'};
    queue/over_budget items carry rank, age_days, dedup, dedup_reason.
    """
    if budget is None:
        budget = int(os.getenv("APPLY_DAILY_BUDGET", str(DEFAULT_BUDGET)))
    now = now or datetime.now()
    liveness_cutoff = now - timedelta(days=LIVENESS_MAX_AGE_DAYS)

    in_flight_job_ids = {
        r["job_id"]
        for r in conn.execute(
            "SELECT job_id FROM application_snapshots WHERE status IN ({})".format(
                ",".join("?" for _ in IN_FLIGHT_SNAPSHOT_STATUSES)
            ),
            IN_FLIGHT_SNAPSHOT_STATUSES,
        )
    }

    eligible, needs_recheck, dead = [], [], []
    for job in fetch_candidates(conn):
        if job["id"] in in_flight_job_ids:
            continue  # already has a snapshot in motion — not queued again
        if job["ats"] in DEAD_ATS:
            dead.append(job)
            continue
        checked = _parse_dt(job["ats_checked_at"])
        if checked is None or checked < liveness_cutoff:
            needs_recheck.append(job)  # JIT re-verify before it may enter the queue
            continue
        eligible.append(job)

    eligible.sort(key=lambda j: sort_key(j, now))

    queue, blocked = [], []
    ctx = DedupContext.from_db(conn)
    for job in eligible:
        verdict, reason = dedup_gate(job, ctx)
        job = {**job, "age_days": job_age_days(job["fetched_at"], now),
               "dedup": verdict, "dedup_reason": reason}
        if verdict == "block":
            blocked.append(job)
        else:
            queue.append(job)

    for rank, job in enumerate(queue, 1):
        job["rank"] = rank

    return {
        "queue": queue[:budget],
        "over_budget": queue[budget:],
        "blocked": blocked,
        "needs_recheck": needs_recheck,
        "dead": dead,
    }


def _print_queue(result: dict, top: int) -> None:
    header = f"{'#':>3} {'gr':<2} {'sc':>3} {'age':>4} {'source':<16} {'company':<26} {'title':<34} {'ats':<14} dedup"
    print(header)
    print("-" * len(header))
    rows = result["queue"] + result["over_budget"]
    budget = len(result["queue"])
    for job in rows[:top]:
        if job["rank"] == budget + 1:
            print(f"--- 預算截斷線（APPLY_DAILY_BUDGET={budget}）---")
        dedup = f"{job['dedup']}: {job['dedup_reason']}" if job["dedup"] != "ok" else ""
        age = f"{job['age_days']}d" if job["age_days"] is not None else "?"
        print(f"{job['rank']:>3} {job['fit_grade']:<2} {job['match_score']:>3} {age:>4} "
              f"{job['source']:<16} {job['company'][:25]:<26} {job['title'][:33]:<34} "
              f"{(job['ats'] or '?'):<14} {dedup}")

    print(f"\n佇列 {len(result['queue'])} 筆（預算內）"
          f"+ {len(result['over_budget'])} 筆超出預算"
          f"｜blocked {len(result['blocked'])}"
          f"｜待 JIT 重驗 {len(result['needs_recheck'])}"
          f"｜dead(殘留) {len(result['dead'])}")
    if result["blocked"]:
        print("\n=== blocked（需人工決定）===")
        for job in result["blocked"]:
            print(f"  {job['fit_grade']} {job['match_score']:>3} {job['company'][:30]:<31} "
                  f"{job['title'][:40]:<41} {job['dedup_reason']}")
    if result["needs_recheck"]:
        print(f"\n=== 待重驗（ats_checked_at 缺失或 >{LIVENESS_MAX_AGE_DAYS} 天）===")
        for job in result["needs_recheck"][:10]:
            print(f"  {job['fit_grade']} {job['match_score']:>3} {job['company'][:30]:<31} "
                  f"{job['title'][:40]:<41} checked_at={job['ats_checked_at'] or 'NULL'}")
        if len(result["needs_recheck"]) > 10:
            print(f"  ... 另 {len(result['needs_recheck']) - 10} 筆")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run the apply queue (zero writes).")
    parser.add_argument("--top", type=int, default=30, help="rows to print (default 30)")
    parser.add_argument("--budget", type=int, default=None,
                        help="override APPLY_DAILY_BUDGET for this run")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        result = build_queue(conn, budget=args.budget)
    finally:
        conn.close()
    _print_queue(result, args.top)


if __name__ == "__main__":
    main()
