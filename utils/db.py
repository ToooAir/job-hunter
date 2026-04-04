import hashlib
import json
import logging
import sqlite3
import os

log = logging.getLogger(__name__)

def _jd_hash(text: str) -> str:
    """MD5 of chars 50–550 of JD text — used for cross-source dedup.

    Skips the first 50 chars to avoid platform boilerplate openings
    (e.g. "We are an equal opportunity employer...") causing false duplicates.
    Falls back to whatever is available if the text is shorter than 50 chars.
    """
    return hashlib.md5(text[50:550].encode()).hexdigest()


SCHEMA_COLUMNS = [
    ("id",                 "TEXT PRIMARY KEY"),
    ("company",            "TEXT NOT NULL"),
    ("title",              "TEXT NOT NULL"),
    ("url",                "TEXT NOT NULL UNIQUE"),
    ("source",             "TEXT NOT NULL"),
    ("source_tier",        "TEXT NOT NULL DEFAULT 'auto'"),
    ("location",           "TEXT"),
    ("raw_jd_text",        "TEXT NOT NULL"),
    ("fetched_at",         "TEXT NOT NULL"),
    ("expires_at",         "TEXT"),
    ("jd_language_req",    "TEXT"),
    ("visa_restriction",   "TEXT"),
    ("salary_range",       "TEXT"),
    ("contract_type",      "TEXT"),
    ("match_score",        "INTEGER"),
    ("fit_grade",          "TEXT"),
    ("top_3_reasons",      "TEXT"),
    ("cover_letter_draft", "TEXT"),
    ("scored_at",          "TEXT"),
    ("applied_at",         "TEXT"),
    ("follow_up_at",       "TEXT"),
    ("jd_hash",            "TEXT"),
    ("notes",              "TEXT"),
    ("interview_brief",    "TEXT"),
    ("company_research",   "TEXT"),
    ("salary_estimate",    "TEXT"),
    ("visa_analysis",      "TEXT"),
    ("translated_jd_text", "TEXT"),
    ("status",             "TEXT NOT NULL DEFAULT 'un-scored'"),
]


INTERVIEW_RECORD_COLUMNS = [
    ("id",             "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("job_id",         "TEXT NOT NULL"),
    ("round",          "TEXT NOT NULL"),   # interview_1 | interview_2 | other
    ("interview_date", "TEXT"),            # ISO date e.g. '2026-04-10'
    ("interviewer",    "TEXT"),            # name / role
    ("format",         "TEXT"),            # phone | video | onsite | technical
    ("questions",      "TEXT"),            # free text
    ("self_rating",    "INTEGER"),         # 1–5
    ("impressions",    "TEXT"),            # free text
    ("created_at",     "TEXT NOT NULL"),
]


def init_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    col_defs = ",\n    ".join(f"{name} {typ}" for name, typ in SCHEMA_COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS jobs (\n    {col_defs}\n);")

    ir_defs = ",\n    ".join(f"{name} {typ}" for name, typ in INTERVIEW_RECORD_COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS interview_records (\n    {ir_defs}\n);")
    conn.commit()

    # Backfill any columns added after initial creation
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col_name, col_type in SCHEMA_COLUMNS:
        if col_name not in existing:
            base_type = col_type.split()[0]
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {base_type}")
    conn.commit()

    return conn


def upsert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """Insert job; return True if newly inserted, False if already existed.

    Cross-source dedup: if the same JD text (first 500 chars) already exists
    under a different URL, skip insertion and log a warning.
    """
    jd_hash = _jd_hash(job.get("raw_jd_text", ""))

    existing = conn.execute(
        "SELECT id, source, url FROM jobs WHERE jd_hash = ?", (jd_hash,)
    ).fetchone()
    if existing:
        log.info(
            "cross-source dup skipped: %s | same JD already in DB as %s (%s)",
            job.get("url", ""),
            existing["source"],
            existing["url"],
        )
        return False

    job_with_hash = {**job, "jd_hash": jd_hash}
    cols = ", ".join(job_with_hash.keys())
    placeholders = ", ".join("?" for _ in job_with_hash)
    sql = (
        f"INSERT INTO jobs ({cols}) VALUES ({placeholders}) "
        "ON CONFLICT(url) DO NOTHING"
    )
    cur = conn.execute(sql, list(job_with_hash.values()))
    conn.commit()
    return cur.rowcount == 1


def fetch_job_by_id(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def reset_to_unscored(conn: sqlite3.Connection, job_ids: list[str]) -> int:
    """Reset specific jobs to un-scored status. Returns count of rows updated."""
    if not job_ids:
        return 0
    placeholders = ",".join("?" for _ in job_ids)
    cur = conn.execute(
        f"UPDATE jobs SET status = 'un-scored' WHERE id IN ({placeholders})",
        job_ids,
    )
    conn.commit()
    return cur.rowcount


def mark_error(conn: sqlite3.Connection, job_id: str, reason: str = "") -> None:
    """Mark a job as permanently failed so Phase 2 won't retry it automatically."""
    conn.execute(
        "UPDATE jobs SET status = 'error', top_3_reasons = ? WHERE id = ?",
        (f"[scoring error] {reason}"[:500] if reason else "[scoring error]", job_id),
    )
    conn.commit()


def mark_expired(conn: sqlite3.Connection, job_ids: list[str]) -> int:
    """Mark jobs as expired (past their expires_at). Returns count updated."""
    if not job_ids:
        return 0
    placeholders = ",".join("?" for _ in job_ids)
    cur = conn.execute(
        f"UPDATE jobs SET status = 'expired' WHERE id IN ({placeholders})",
        job_ids,
    )
    conn.commit()
    return cur.rowcount


def get_unscored_jobs(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM jobs WHERE status = 'un-scored'")
    return [dict(row) for row in cur.fetchall()]


def update_score(conn: sqlite3.Connection, job_id: str, result: dict) -> None:
    top3 = result.get("top_3_reasons", [])
    if isinstance(top3, list):
        top3 = json.dumps(top3, ensure_ascii=False)
    conn.execute(
        """UPDATE jobs
           SET match_score = ?,
               fit_grade = ?,
               top_3_reasons = ?,
               cover_letter_draft = ?,
               jd_language_req = ?,
               visa_restriction = ?,
               salary_range = ?,
               contract_type = ?,
               scored_at = ?,
               status = 'scored'
           WHERE id = ?""",
        (
            result.get("match_score"),
            result.get("fit_grade"),
            top3,
            result.get("cover_letter_draft"),
            result.get("jd_language_req"),
            result.get("visa_restriction"),
            result.get("salary_range"),
            result.get("contract_type"),
            result.get("scored_at"),
            job_id,
        ),
    )
    conn.commit()


def update_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    applied_at: str = None,
) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, applied_at = ? WHERE id = ?",
        (status, applied_at if status == "applied" else None, job_id),
    )
    conn.commit()


def set_notes(conn: sqlite3.Connection, job_id: str, notes: str) -> None:
    conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))
    conn.commit()


def set_translated_jd(conn: sqlite3.Connection, job_id: str, text: str) -> None:
    conn.execute("UPDATE jobs SET translated_jd_text = ? WHERE id = ?", (text, job_id))
    conn.commit()


def set_visa_analysis(conn: sqlite3.Connection, job_id: str, analysis: str) -> None:
    conn.execute("UPDATE jobs SET visa_analysis = ? WHERE id = ?", (analysis, job_id))
    conn.commit()


def set_salary_estimate(conn: sqlite3.Connection, job_id: str, estimate: str) -> None:
    conn.execute("UPDATE jobs SET salary_estimate = ? WHERE id = ?", (estimate, job_id))
    conn.commit()


def set_company_research(conn: sqlite3.Connection, job_id: str, research: str) -> None:
    conn.execute("UPDATE jobs SET company_research = ? WHERE id = ?", (research, job_id))
    conn.commit()


def update_cover_letter(conn: sqlite3.Connection, job_id: str, text: str) -> None:
    conn.execute("UPDATE jobs SET cover_letter_draft = ? WHERE id = ?", (text, job_id))
    conn.commit()


def set_interview_brief(conn: sqlite3.Connection, job_id: str, brief: str) -> None:
    conn.execute("UPDATE jobs SET interview_brief = ? WHERE id = ?", (brief, job_id))
    conn.commit()


def add_interview_record(conn: sqlite3.Connection, record: dict) -> int:
    """Insert one interview record. Returns the new row id."""
    cols = ", ".join(record.keys())
    placeholders = ", ".join("?" for _ in record)
    cur = conn.execute(
        f"INSERT INTO interview_records ({cols}) VALUES ({placeholders})",
        list(record.values()),
    )
    conn.commit()
    return cur.lastrowid


def get_interview_records(conn: sqlite3.Connection, job_id: str) -> list[dict]:
    """Return all interview records for a job, oldest first."""
    rows = conn.execute(
        "SELECT * FROM interview_records WHERE job_id = ? ORDER BY interview_date, created_at",
        (job_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_interview_record(conn: sqlite3.Connection, record_id: int) -> None:
    conn.execute("DELETE FROM interview_records WHERE id = ?", (record_id,))
    conn.commit()


def set_follow_up(conn: sqlite3.Connection, job_id: str, follow_up_at: str | None) -> None:
    """Set or clear the follow-up reminder date (ISO8601 date string, e.g. '2026-04-09')."""
    conn.execute(
        "UPDATE jobs SET follow_up_at = ? WHERE id = ?",
        (follow_up_at, job_id),
    )
    conn.commit()
