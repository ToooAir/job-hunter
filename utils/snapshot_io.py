"""snapshot_io.py — check-out / check-in I/O between drafts and submission.

Step 5.1. The submission host (apply_session.py) and the review UI talk to
the snapshot queue only through this module; if the pipeline ever moves to
a VPS, this is the single layer to swap for a fetch/deliver exchange — the
callers don't change.

Lifecycle (DB-as-queue): draft / approved / submitted are IN_FLIGHT and
keep the job out of the apply queue. Marking a snapshot *failed* (or
abandoned) releases the job: the next Stage 1 run regenerates a fresh
draft against the live page and it goes through review again — that loop
IS the drift recovery story, so failures must always land here with a
reason in notes.

Legal transitions enforced here:
    draft    → approved | abandoned | submitted*   (*Tier 3 watch mode:
               the human submits a never-approved draft; the watcher
               books it as submitted_by='human')
    approved → submitted | failed | abandoned
"""

from __future__ import annotations

import json
import sqlite3

from utils.db import (
    _now_local_iso,
    update_application_snapshot,
    update_status,
)

_JSON_FIELDS = ("form_payload", "custom_qa", "verifier_report")

_ALLOWED_TRANSITIONS = {
    "approved": ("draft",),
    "abandoned": ("draft", "approved"),
    "submitted": ("draft", "approved"),
    "failed": ("draft", "approved"),
}


def _decode(snap: dict) -> dict:
    for key in _JSON_FIELDS:
        if isinstance(snap.get(key), str) and snap[key]:
            try:
                snap[key] = json.loads(snap[key])
            except ValueError:
                pass  # leave the raw string for a human to look at
    return snap


def _get(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM application_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no snapshot with id {snapshot_id}")
    return dict(row)


def _append_note(existing: str | None, note: str) -> str:
    line = f"[{_now_local_iso()}] {note}"
    return f"{existing}\n{line}" if existing else line


def _transition(conn, snap: dict, new_status: str, note: str | None = None,
                **fields) -> None:
    if snap["status"] not in _ALLOWED_TRANSITIONS[new_status]:
        raise ValueError(
            f"snapshot {snap['id']}: illegal transition "
            f"{snap['status']!r} → {new_status!r}")
    if note:
        fields["notes"] = _append_note(snap.get("notes"), note)
    update_application_snapshot(conn, snap["id"], status=new_status, **fields)


# ── check-out ──────────────────────────────────────────────────────────────────

def fetch_work(conn: sqlite3.Connection, status: str = "approved") -> list[dict]:
    """Snapshots ready for a submission session, oldest approval first.

    JSON fields come back decoded and each snapshot carries a 'job' dict
    (title/company/url/status) for dedup re-checks and session pacing."""
    rows = conn.execute(
        """SELECT s.*, j.title AS j_title, j.company AS j_company,
                  j.url AS j_url, j.status AS j_status,
                  j.match_score AS j_match_score, j.fit_grade AS j_fit_grade
           FROM application_snapshots s JOIN jobs j ON j.id = s.job_id
           WHERE s.status = ?
           ORDER BY COALESCE(s.approved_at, s.created_at), s.id""",
        (status,),
    ).fetchall()
    work = []
    for row in rows:
        snap = dict(row)
        snap["job"] = {"title": snap.pop("j_title"),
                       "company": snap.pop("j_company"),
                       "url": snap.pop("j_url"),
                       "status": snap.pop("j_status"),
                       "match_score": snap.pop("j_match_score"),
                       "fit_grade": snap.pop("j_fit_grade")}
        work.append(_decode(snap))
    return work


def last_failure(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Newest failed snapshot for a job — the review page shows its notes
    next to the regenerated draft ('why did the last attempt bounce')."""
    row = conn.execute(
        """SELECT id, created_at, notes, screenshot_path
           FROM application_snapshots
           WHERE job_id = ? AND status = 'failed'
           ORDER BY id DESC LIMIT 1""",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


# ── review decisions (dashboard, 5.2) ──────────────────────────────────────────

def approve_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> None:
    _transition(conn, _get(conn, snapshot_id), "approved",
                approved_at=_now_local_iso())


def abandon_snapshot(conn: sqlite3.Connection, snapshot_id: int,
                     reason: str = "") -> None:
    _transition(conn, _get(conn, snapshot_id), "abandoned",
                note=f"abandoned: {reason}" if reason else "abandoned")


def _abandon_sibling_drafts(conn: sqlite3.Connection, submitted_snap: dict) -> list[int]:
    """Abandon other in-flight drafts/approved snapshots for the SAME company.

    The manual cheat-sheet path has no session-level dedup, so a second channel
    for a company we just applied to would otherwise linger in the review queue
    inviting a duplicate submit (watchlist #2 — already bit us once with
    Matrix42). Company match uses the same suffix-stripping normaliser as the
    apply queue's dedup gate. Returns the ids it abandoned."""
    from utils.apply_queue import normalize_company  # pure; lazy to avoid cycle

    row = conn.execute(
        "SELECT company FROM jobs WHERE id = ?", (submitted_snap["job_id"],)
    ).fetchone()
    if row is None:
        return []
    target = normalize_company(row["company"])
    if not target:
        return []
    siblings = conn.execute(
        """SELECT s.id, j.company
           FROM application_snapshots s JOIN jobs j ON j.id = s.job_id
           WHERE s.status IN ('draft', 'approved') AND s.id != ?""",
        (submitted_snap["id"],),
    ).fetchall()
    abandoned = []
    for sib in siblings:
        if normalize_company(sib["company"]) == target:
            abandon_snapshot(
                conn, sib["id"],
                reason=f"company already applied via snapshot #{submitted_snap['id']}")
            abandoned.append(sib["id"])
    return abandoned


# ── check-in (apply_session, 5.3) ──────────────────────────────────────────────

def report_result(
    conn: sqlite3.Connection,
    snapshot_id: int,
    outcome: str,
    note: str = "",
    screenshot_path: str | None = None,
    submitted_by: str = "agent",
) -> list[int]:
    """Book a session outcome for one snapshot.

    outcome:
      'submitted' — also flips the job to applied (applied_at = now), the one
                    place job status and snapshot move together; additionally
                    abandons sibling drafts for the same company (returns their
                    ids) so a manual second channel can't be double-submitted
      'failed'    — snapshot leaves IN_FLIGHT, the job re-queues; a reason
                    note is mandatory (never fail silently)
      'prepared'  — prepare mode: form filled, tab left for the human;
                    snapshot stays approved, screenshot/notes recorded

    Returns the list of sibling snapshot ids abandoned (empty unless submitted).
    """
    snap = _get(conn, snapshot_id)
    extra = {"screenshot_path": screenshot_path} if screenshot_path else {}

    if outcome == "submitted":
        now = _now_local_iso()
        _transition(conn, snap, "submitted",
                    note=note or None, submitted_at=now,
                    submitted_by=submitted_by, **extra)
        update_status(conn, snap["job_id"], "applied", applied_at=now)
        return _abandon_sibling_drafts(conn, snap)
    elif outcome == "failed":
        if not note:
            raise ValueError("a failed result needs a reason note")
        _transition(conn, snap, "failed", note=f"failed: {note}", **extra)
    elif outcome == "prepared":
        fields = dict(extra)
        fields["notes"] = _append_note(snap.get("notes"),
                                       note or "prepared: left for human")
        update_application_snapshot(conn, snapshot_id, **fields)
    else:
        raise ValueError(f"unknown outcome {outcome!r}")
    return []
