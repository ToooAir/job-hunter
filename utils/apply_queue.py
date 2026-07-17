"""apply_queue.py — build the daily semi-auto apply queue (Step 2).

Pure Python over the jobs/application_snapshots tables; no LLM, no network.

Eligibility (all must hold):
  * status = 'scored'  (pipeline/skipped/expired/error are therefore out)
  * located in Germany, or a Germany-eligible remote label (Remote — EU)
  * fit_grade A, or B with match_score >= MIN_B_SCORE (65)
  * no in-flight snapshot for the job itself (draft/approved/submitted)
  * liveness: ats_checked_at <= LIVENESS_MAX_AGE_DAYS and ats not in DEAD_ATS;
    stale/unchecked candidates land in `needs_recheck` — they re-enter after a
    JIT re-verify (ats_scan), they are NOT silently dropped

Ranking (deterministic, applied in order):
  1. freshness bucket — fetched_at <= FRESH_BUCKET_DAYS first (protects
     perishable sources like wearedevelopers, whose jobs die young)
  2. grade A before B
  3. addressable bucket — extension-fillable ATS first (env APPLY_PREFER_
     ADDRESSABLE, default on); a gentle bias, never lets a B jump an A
  4. match_score descending
  5. fetched_at newest first

Experiment gates (both default off) for the résumé-conversion cohort — apply
only to high-fit jobs the extension can actually submit to, then measure the
interview rate with utils.resume_stats:
  APPLY_MIN_SCORE=85       drop candidates below this match_score
  APPLY_ADDRESSABLE_ONLY=1 keep only extension-fillable ATS (is_addressable)

Dedup gate (per candidate, on normalized company name):
  block — company already has a pipeline record (a ghosted one expires after
          APPLY_GHOST_COOLDOWN_DAYS, a rejected one after
          APPLY_REJECT_COOLDOWN_DAYS), another job of the same company has an
          in-flight snapshot, or the exact title was previously rejected
  warn  — same jd_hash already applied under another job (recruiter repost),
          second job of the same company within this batch, or a different
          role at a company whose rejection cooled off

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

# Apply-effort score gate for grade B — intentionally distinct from the scorer's
# grade boundary (B = score 60–74). The grade is a display/triage band; the queue
# spends LLM draft-generation + human review on each job, so it takes only B at or
# above this bar. Single source of truth: ats_scan imports it so the scan pool and
# the queue pool can never diverge. Lowered 70 → 65 (2026-07-13): the LLM quantizes
# scores into ~55/65/72+ buckets, so 65 admits its genuine "borderline-pass" tier
# (verified not a source-bonus artifact) to feed the supply-starved queue.
MIN_B_SCORE = 65

# Geo-triage remote labels we can actually apply to (Chancenkarte → EU-remote is
# workable). "Remote — Germany" already matches GERMANY_KEYWORDS via "German";
# "Remote — non-EU" / "Remote — unclear" are deliberately excluded.
REMOTE_ELIGIBLE_LOCATIONS = ("Remote — EU",)

# Student/intern roles can never be applied to (experienced-hire search) — the
# scorer let a "Internship/Master Thesis" posting through to a Tier-3 draft
# (snapshot 159, abandoned 2026-07-10). \b keeps "intern" from matching
# "international". phase2_scorer shares this as a pre-flight, so unscored
# student roles also skip the LLM spend.
TITLE_EXCLUDE_RE = re.compile(
    r"\b(intern|interns|internship|praktikum|praktikant(?:in)?|"
    r"werkstudent(?:in)?|working\s+student|thesis|masterarbeit|"
    r"bachelorarbeit|abschlussarbeit|ausbildung|azubi|apprentice(?:ship)?|"
    r"duales?\s+studium)\b", re.I)


def title_excluded(title: str | None) -> bool:
    """True when the job title marks a student/intern role we never apply to."""
    return bool(TITLE_EXCLUDE_RE.search(title or ""))

# A ghosted company never actually rejected us — after this cooldown a *new* role
# there is fair game again. applied/interview/offer stay permanently
# blocked. Env override: APPLY_GHOST_COOLDOWN_DAYS.
GHOST_COOLDOWN_DAYS = 60

# A rejection was about one role, not the whole company — after this cooldown a
# *different* role there re-enters the queue as a warn (human decides), while
# the same title stays permanently blocked (a repost of the role that said no).
# Env override: APPLY_REJECT_COOLDOWN_DAYS.
REJECT_COOLDOWN_DAYS = 90

# ATS the browser extension can actually auto-fill (native inputs, no custom-JS
# widget/dropzone/captcha wall). join/indeed/softgarden are deliberately absent —
# three field tests proved they die at field-extraction or fill or reCAPTCHA and
# stay a human floor. See memory extension-autofill-spike.
ADDRESSABLE_ATS = frozenset({"greenhouse", "lever", "ashby", "workable", "personio"})

# Structured ATS often hides behind a job board / redirect / iframe, so the `ats`
# column reads "unknown"/"unknown-external". These apply_url fingerprints pierce
# the disguise (e.g. Workato's careers page whose form is Greenhouse via gh_jid).
_ADDRESSABLE_URL_PATTERNS = (
    "greenhouse.io", "gh_jid=", "grnh.se",
    "lever.co",
    "ashbyhq.com",
    "personio.de", "personio.com", "jobs.personio",
    "workable.com",
)


def is_addressable(job: dict) -> bool:
    """True when the extension can auto-fill this application, incl. structured
    ATS disguised behind a board/redirect/iframe (detected via apply_url).
    Everything else (join custom-JS, indeed account-wall, captcha) is human-only."""
    if (job.get("ats") or "").lower() in ADDRESSABLE_ATS:
        return True
    url = (job.get("apply_url") or "").lower()
    return any(p in url for p in _ADDRESSABLE_URL_PATTERNS)

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


def _norm_title(title: str | None) -> str:
    """Whitespace-collapsed lowercase title, for same-role comparisons."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


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


def sort_key(job: dict, now: datetime, prefer_addressable: bool = False):
    age = job_age_days(job["fetched_at"], now)
    fresh_bucket = 0 if age is not None and age <= FRESH_BUCKET_DAYS else 1
    grade_rank = 0 if job["fit_grade"] == "A" else 1
    # Among same-freshness, same-grade jobs, float ones the extension can actually
    # submit to. Never lets a B jump an A — a gentle bias, not an override.
    addr_bucket = 0 if (prefer_addressable and is_addressable(job)) else 1
    fetched = _parse_dt(job["fetched_at"]) or datetime.min
    return (fresh_bucket, grade_rank, addr_bucket,
            -(job["match_score"] or 0), -fetched.timestamp())


class DedupContext:
    """Company/jd_hash state the gate checks against; built once per queue run."""

    def __init__(self, pipeline_companies, in_flight, applied_jd_hashes,
                 cooled_rejected=None, rejected_titles=None,
                 reject_cooldown_days=REJECT_COOLDOWN_DAYS):
        self.pipeline_companies = pipeline_companies      # norm company -> status
        self.in_flight = in_flight                        # norm company -> job_id
        self.applied_jd_hashes = applied_jd_hashes        # jd_hash -> job_id
        self.cooled_rejected = cooled_rejected or set()   # norm company (rejection cooled off)
        self.rejected_titles = rejected_titles or set()   # (norm company, norm title)
        self.reject_cooldown_days = reject_cooldown_days
        self.batch_companies: dict[str, str] = {}         # norm company -> job_id (this batch)
        # (norm company, norm title) -> job_id — multi-city variants of one
        # posting (Breuninger ×3, heise/gtj city-suffixed reposts) collapse to
        # one draft per batch instead of one per city
        self.batch_titles: dict[tuple[str, str], str] = {}

    @classmethod
    def from_db(cls, conn, now: datetime | None = None):
        now = now or datetime.now()
        placeholders = ",".join("?" for _ in PIPELINE_STATUSES)
        cooldown_days = int(os.getenv("APPLY_GHOST_COOLDOWN_DAYS", str(GHOST_COOLDOWN_DAYS)))
        ghost_cutoff = now - timedelta(days=cooldown_days)
        reject_days = int(os.getenv("APPLY_REJECT_COOLDOWN_DAYS", str(REJECT_COOLDOWN_DAYS)))
        reject_cutoff = now - timedelta(days=reject_days)
        pipeline: dict[str, str] = {}
        cooled_rejected: set[str] = set()
        rejected_titles: set[tuple[str, str]] = set()
        for r in conn.execute(
            f"SELECT company, title, status, applied_at FROM jobs WHERE status IN ({placeholders})",
            PIPELINE_STATUSES,
        ):
            company = normalize_company(r["company"])
            # A company we ghosted before the cooldown never rejected us — release
            # it so a new role there can be applied to. Unknown applied_at (should
            # not happen for ghosted) fails closed: keep blocking.
            if r["status"] == "ghosted":
                applied = _parse_dt(r["applied_at"])
                if applied is not None and applied < ghost_cutoff:
                    continue
            if r["status"] == "rejected":
                # The rejected role's title blocks forever (a repost is the same
                # "no"); the company itself cools off after the cooldown into a
                # warn — a different role there is a human decision, not a block.
                # Unknown applied_at fails closed: keep blocking.
                title = _norm_title(r["title"])
                if title:
                    rejected_titles.add((company, title))
                applied = _parse_dt(r["applied_at"])
                if applied is not None and applied < reject_cutoff:
                    cooled_rejected.add(company)
                    continue
            pipeline[company] = r["status"]
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
        return cls(pipeline, in_flight, applied_hashes,
                   cooled_rejected=cooled_rejected, rejected_titles=rejected_titles,
                   reject_cooldown_days=reject_days)


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

    title = _norm_title(job.get("title"))
    if title and (company, title) in ctx.rejected_titles:
        # even after the company's rejection cooled off, the exact role that
        # said no stays out — a repost of it is the same "no"
        return "block", "same title previously rejected"
    if title and (company, title) in ctx.batch_titles:
        # same company AND same title in one batch = a multi-city variant of
        # one posting — a second draft is pure generation + review noise (the
        # sibling's submit auto-revokes it anyway). A genuinely different role
        # at the same company stays a warn below, not a block.
        return "block", (f"same-title variant in batch "
                         f"(job {ctx.batch_titles[(company, title)]})")

    verdict, reason = "ok", ""
    if job.get("jd_hash") and job["jd_hash"] in ctx.applied_jd_hashes:
        verdict, reason = "warn", f"same JD applied as job {ctx.applied_jd_hashes[job['jd_hash']]}"
    elif company in ctx.batch_companies:
        verdict, reason = "warn", f"2nd job of company in batch (job {ctx.batch_companies[company]})"
    elif company in ctx.cooled_rejected:
        verdict, reason = "warn", (f"rejected >{ctx.reject_cooldown_days}d ago — "
                                   "different role, human decides")
    ctx.batch_companies.setdefault(company, job["id"])
    if title:
        ctx.batch_titles.setdefault((company, title), job["id"])
    return verdict, reason


def fetch_candidates(conn) -> list[dict]:
    """Grade/score/location/status-eligible jobs, before liveness and dedup."""
    loc_terms = [f"location LIKE '%{kw}%'" for kw in GERMANY_KEYWORDS]
    loc_terms += [f"location = '{loc}'" for loc in REMOTE_ELIGIBLE_LOCATIONS]
    loc_clause = " OR ".join(loc_terms)
    rows = conn.execute(
        "SELECT id, source, company, title, url, location, fit_grade, match_score, "
        "       fetched_at, jd_hash, ats, apply_url, ats_checked_at "
        "FROM jobs "
        "WHERE status = 'scored' "
        f"  AND (fit_grade = 'A' OR (fit_grade = 'B' AND match_score >= {MIN_B_SCORE})) "
        f" AND ({loc_clause})"
    )
    return [dict(r) for r in rows if not title_excluded(r["title"])]


def topup_budget(live_count: int, target: int) -> int:
    """How many new drafts to generate to refill the live-draft pool to `target`.
    Inventory model: never negative, never over-fills."""
    return max(0, target - live_count)


def build_queue(conn, budget: int | None = None, now: datetime | None = None,
                include_stale: bool = False) -> dict:
    """Build the ranked apply queue. Read-only: never writes any status.

    Returns {'queue', 'over_budget', 'blocked', 'needs_recheck', 'dead'};
    queue/over_budget items carry rank, age_days, dedup, dedup_reason.

    include_stale: when True, candidates whose ats_checked_at is stale/missing
    enter the queue instead of needs_recheck — Stage 1's Pass A re-verifies them
    on the way in (expiring the dead before any LLM), so the inventory top-up is
    not starved by an aged backlog.
    """
    if budget is None:
        budget = int(os.getenv("APPLY_DAILY_BUDGET", str(DEFAULT_BUDGET)))
    now = now or datetime.now()
    liveness_cutoff = now - timedelta(days=LIVENESS_MAX_AGE_DAYS)

    # Ranking bias toward extension-fillable ATS (default on).
    prefer_addressable = os.getenv("APPLY_PREFER_ADDRESSABLE", "1") != "0"
    # Experiment gates (default off): the résumé-conversion cohort is "85+ score,
    # extension-fillable only". Set both to emit exactly that batch.
    addressable_only = os.getenv("APPLY_ADDRESSABLE_ONLY", "0") == "1"
    min_score = int(os.getenv("APPLY_MIN_SCORE", "0"))

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
        if (job["match_score"] or 0) < min_score:
            continue  # experiment cohort: below the score floor
        if addressable_only and not is_addressable(job):
            continue  # experiment cohort: extension-fillable ATS only
        if job["ats"] in DEAD_ATS:
            dead.append(job)
            continue
        checked = _parse_dt(job["ats_checked_at"])
        is_stale = checked is None or checked < liveness_cutoff
        if is_stale and not include_stale:
            needs_recheck.append(job)  # JIT re-verify before it may enter the queue
            continue
        eligible.append(job)  # stale ones included only when include_stale (Pass A verifies)

    eligible.sort(key=lambda j: sort_key(j, now, prefer_addressable))

    queue, blocked = [], []
    ctx = DedupContext.from_db(conn, now=now)
    for job in eligible:
        verdict, reason = dedup_gate(job, ctx)
        job = {**job, "age_days": job_age_days(job["fetched_at"], now),
               "dedup": verdict, "dedup_reason": reason,
               "addressable": is_addressable(job)}
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
    header = f"{'#':>3} {'gr':<2} {'sc':>3} {'age':>4} {'fill':<4} {'source':<16} {'company':<26} {'title':<34} {'ats':<14} dedup"
    print(header)
    print("-" * len(header))
    rows = result["queue"] + result["over_budget"]
    budget = len(result["queue"])
    addr_n = sum(1 for j in result["queue"] if j.get("addressable"))
    for job in rows[:top]:
        if job["rank"] == budget + 1:
            print(f"--- 預算截斷線（APPLY_DAILY_BUDGET={budget}）---")
        dedup = f"{job['dedup']}: {job['dedup_reason']}" if job["dedup"] != "ok" else ""
        age = f"{job['age_days']}d" if job["age_days"] is not None else "?"
        fill = "✓" if job.get("addressable") else "·"
        print(f"{job['rank']:>3} {job['fit_grade']:<2} {job['match_score']:>3} {age:>4} "
              f"{fill:<4} {job['source']:<16} {job['company'][:25]:<26} {job['title'][:33]:<34} "
              f"{(job['ats'] or '?'):<14} {dedup}")
    print(f"\n可觸及 ATS（套件可投）：{addr_n}/{len(result['queue'])} 筆在預算內")

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
