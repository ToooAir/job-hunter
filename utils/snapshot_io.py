"""snapshot_io.py — the draft queue the review UI reads and writes.

The dashboard talks to the snapshot queue only through this module. Stage 1
generates a draft (read the page, write the cover letter / answers, verify);
the human reviews it in the dashboard, copies the answer sheet onto the real
application form themselves, and marks it submitted. There is no automated
submission step.

Lifecycle (DB-as-queue): draft / submitted are IN_FLIGHT and keep the job out
of the apply queue. Abandoning a snapshot releases the job: the next Stage 1
run regenerates a fresh draft against the live page and it returns to review.

Legal transitions enforced here:
    draft → submitted   (the human applied on the real site, marks it done)
    draft → abandoned   (skip; the job re-queues for a fresh draft)
"""

from __future__ import annotations

import json
import sqlite3

from utils.db import (
    _now_local_iso,
    clear_focus,
    update_application_snapshot,
    update_status,
)

_JSON_FIELDS = ("form_payload", "custom_qa", "verifier_report")

_ALLOWED_TRANSITIONS = {
    "abandoned": ("draft",),
    "submitted": ("draft",),
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


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    """One snapshot, JSON fields (form_payload/custom_qa/verifier_report)
    decoded. Raises ValueError if the id is unknown."""
    return _decode(_get(conn, snapshot_id))


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

def fetch_work(conn: sqlite3.Connection, status: str = "draft") -> list[dict]:
    """Snapshots in the given lifecycle state, oldest first.

    JSON fields come back decoded and each snapshot carries a 'job' dict
    (title/company/url/status) for dedup re-checks and review display."""
    rows = conn.execute(
        """SELECT s.*, j.title AS j_title, j.company AS j_company,
                  j.url AS j_url, j.status AS j_status,
                  j.match_score AS j_match_score, j.fit_grade AS j_fit_grade,
                  j.ats_checked_at AS j_ats_checked_at,
                  j.cover_letter_draft AS j_cover_letter_draft
           FROM application_snapshots s JOIN jobs j ON j.id = s.job_id
           WHERE s.status = ?
           ORDER BY s.created_at, s.id""",
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
                       "fit_grade": snap.pop("j_fit_grade"),
                       "ats_checked_at": snap.pop("j_ats_checked_at"),
                       "cover_letter_draft": snap.pop("j_cover_letter_draft")}
        work.append(_decode(snap))
    return work


# ── review decisions (dashboard, 5.2) ──────────────────────────────────────────

def abandon_snapshot(conn: sqlite3.Connection, snapshot_id: int,
                     reason: str = "") -> None:
    _transition(conn, _get(conn, snapshot_id), "abandoned",
                note=f"abandoned: {reason}" if reason else "abandoned")


def append_custom_qa(conn: sqlite3.Connection, snapshot_id: int,
                     question: str, answer: str,
                     source: str = "on-demand") -> None:
    """Append one Q&A to the snapshot's evidence trail (answer panel).

    Unlike edit_snapshot this is a data append, not a review transition:
    submitted rows accept it too — the interview-prep trail keeps growing
    as questions are actually met on the form."""
    snap = _get(conn, snapshot_id)
    try:
        qa = json.loads(snap.get("custom_qa") or "[]")
    except ValueError:
        qa = []
    qa.append({"question": question, "answer": answer, "source": source,
               "asked_at": _now_local_iso()})
    update_application_snapshot(
        conn, snapshot_id, custom_qa=json.dumps(qa, ensure_ascii=False))


def edit_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    cover_letter: str | None = None,
    action_values: dict | None = None,
    note: str = "edited in review",
) -> list[str]:
    """Apply a human's review edits to a DRAFT snapshot before approval.

    A reviewer often needs to fix one word (a wrong 'where did you hear about
    us' value, an overstated line in the cover letter) on an otherwise good
    draft; without this the only options are approve-as-is or abandon-and-
    regenerate. Human-edited text is reviewed text by definition, so the
    verifier is NOT re-run — the stored verifier_report stays as the record of
    what was originally flagged.

    cover_letter   — full replacement text; also written into the bound
                     cover-letter action (source=="cover_letter") so the value
                     the host fills stays in sync with the displayed letter.
    action_values  — {selector: new_value} for specific form fields.
    Only DRAFT snapshots are editable (approved/submitted are frozen). Returns
    the labels actually changed (empty if nothing differed)."""
    snap = _get(conn, snapshot_id)
    if snap["status"] != "draft":
        raise ValueError(
            f"snapshot {snapshot_id} not editable in status {snap['status']!r}")
    payload = json.loads(snap.get("form_payload") or "{}")
    actions = payload.get("actions") or []
    changed: list[str] = []
    fields: dict = {}

    for a in actions:
        sel = a.get("selector")
        if action_values and sel in action_values \
                and action_values[sel] != a.get("value"):
            a["value"] = action_values[sel]
            changed.append(a.get("label") or sel)

    if cover_letter is not None and cover_letter != (snap.get("cover_letter") or ""):
        fields["cover_letter"] = cover_letter
        for a in actions:
            if a.get("source") == "cover_letter":
                a["value"] = cover_letter
        changed.append("cover letter")

    if not changed:
        return []
    payload["actions"] = actions
    fields["form_payload"] = payload
    fields["notes"] = _append_note(snap.get("notes"),
                                   f"{note}: {', '.join(changed)}")
    update_application_snapshot(conn, snapshot_id, **fields)
    return changed


def _abandon_sibling_drafts(conn: sqlite3.Connection, submitted_snap: dict) -> list[int]:
    """Abandon other draft snapshots for the SAME company on submit.

    The manual path has no session-level dedup, so a second channel for a
    company we just applied to would otherwise linger in the review queue
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
           WHERE s.status = 'draft' AND s.id != ?""",
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


def reconcile_applied_job(conn: sqlite3.Connection, job_id: str) -> list[int]:
    """Reconcile the review queue after an application booked OUTSIDE it.

    The dashboard's apply button writes jobs.applied_at directly (no snapshot
    transition), so a draft for that very job — or for the same company —
    kept sitting in the review queue with no warning (GWQ snapshot #95 bit
    us on 2026-07-08). Abandon those drafts here, mirroring what
    mark_submitted does for the in-queue path. Returns the abandoned ids."""
    from utils.apply_queue import normalize_company  # pure; lazy to avoid cycle

    row = conn.execute(
        "SELECT company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    target = normalize_company(row["company"]) if row else ""
    drafts = conn.execute(
        """SELECT s.id, s.job_id, j.company
           FROM application_snapshots s JOIN jobs j ON j.id = s.job_id
           WHERE s.status = 'draft'""").fetchall()
    abandoned = []
    for d in drafts:
        if d["job_id"] == job_id:
            reason = "job applied outside the review queue"
        elif target and normalize_company(d["company"]) == target:
            reason = f"company already applied (job {job_id})"
        else:
            continue
        abandon_snapshot(conn, d["id"], reason=reason)
        clear_focus(conn, snapshot_id=d["id"])  # no-op unless it was focused
        abandoned.append(d["id"])
    return abandoned


def mark_submitted(conn: sqlite3.Connection, snapshot_id: int,
                   note: str = "") -> list[int]:
    """Record that the human applied on the real site: snapshot → submitted,
    job → applied (the one place job status and snapshot move together), and
    abandon sibling drafts for the same company so a second channel can't be
    double-submitted. Returns the abandoned sibling ids."""
    snap = _get(conn, snapshot_id)
    now = _now_local_iso()
    _transition(conn, snap, "submitted", note=note or None,
                submitted_at=now, submitted_by="human")
    update_status(conn, snap["job_id"], "applied", applied_at=now)
    # the answer panel's focus is spent once its application is submitted
    # (no-op when the focus has already moved on to another draft)
    clear_focus(conn, snapshot_id=snapshot_id)
    return _abandon_sibling_drafts(conn, snap)
