#!/usr/bin/env python3
"""apply_api.py — the local sidecar the autofill extension talks to.

Phase 1 (extension/PHASE1_PLAN.md): a token-gated HTTP API on host-loopback that
lets the browser extension pull the right reviewed draft for the page it is on,
fetch the CV bytes to upload, and mark the application submitted on confirmation.

Reuses utils.snapshot_io / utils.db — no new data model. Read-only except the
single `submitted` POST, which only advances the existing draft→submitted
lifecycle transition (and books the job applied + abandons same-company siblings,
via mark_submitted).

`POST /fill-plan` is snapshot-free: the extension sends fields it live-extracted
from any page (incl. forms Stage 1 never saw) and gets back which map to a
profile fact. Facts are job-independent, so no job_id/snapshot is needed; open
questions have no fact and stay blank (never invented). See memory
snapshot-free-autofill.

Security: bind to 127.0.0.1 only (compose publishes 127.0.0.1:8531), require a
bearer token from APPLY_API_TOKEN. It serves the CV + personal data, so it must
never be exposed beyond host-loopback.

Run (in the container, via compose service `apply_api`):
    uvicorn apply_api:app --host 0.0.0.0 --port 8531
"""

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from utils.db import init_db
from utils.profile_loader import load_profile
from utils.snapshot_io import get_snapshot, mark_submitted

app = FastAPI(title="job-hunter apply api", version="1.0")

# The extension's background worker should reach us via host_permissions (no
# CORS), but a not-fully-applied host grant makes the Authorization header
# trigger a preflight that would otherwise fail. Permissive CORS is safe here:
# the service is bound to 127.0.0.1 and every route is token-gated anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    # the CV filename rides in Content-Disposition; CORS hides non-safelisted
    # response headers from JS unless we expose them (else the extension sees a
    # null header and falls back to "cv.pdf").
    expose_headers=["Content-Disposition"],
)


# ── auth & db (both resolved per-request so tests/env can set them late) ───────
def require_token(authorization: str = Header(default="")) -> None:
    token = os.getenv("APPLY_API_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="APPLY_API_TOKEN not set")
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid or missing token")


def _conn():
    # one connection per request; SQLite WAL handles concurrent readers + the
    # one writer (this POST) alongside the pipeline/dashboard containers.
    return init_db(os.getenv("DB_PATH", "./data/jobs.db"))


@lru_cache(maxsize=1)
def _cv_path() -> Path:
    """The candidate's CV path (strict profile load). Cached — one CV per
    candidate. Tests monkeypatch this to avoid loading the real profile."""
    return load_profile().cv_path


@lru_cache(maxsize=1)
def _profile():
    """The candidate profile (facts + aliases). Cached; tests monkeypatch this."""
    return load_profile()


# ── /fill-plan (snapshot-free) ───────────────────────────────────────────────
class FillField(BaseModel):
    """One live-extracted form field the extension found on the page."""
    id: str = ""     # client-side element key, echoed back — `name` alone is
    #                  ambiguous (radio groups share one name across options)
    label: str = ""
    name: str = ""
    type: str = "text"
    options: list[str] | None = None


class FillPlanRequest(BaseModel):
    fields: list[FillField]


def _resolve_option(value: str, options: list[str] | None) -> tuple[str, bool]:
    """Map a profile value onto a <select>'s real option text. Returns
    (option_text, needs_review). No match → keep the value but flag it, so a
    dropdown mismatch (the German 'Bitte wählen' case) never fills silently."""
    if not options:
        return value, False
    vlow = value.strip().lower()
    for opt in options:                       # exact first
        if opt.strip().lower() == vlow:
            return opt, False
    for opt in options:                       # then containment either way
        olow = opt.strip().lower()
        if olow and (vlow in olow or olow in vlow):
            return opt, False
    return value, True                        # nothing fit → human decides


# ── endpoints ──────────────────────────────────────────────────────────────────
@app.get("/pending", dependencies=[Depends(require_token)])
def pending():
    """Draft snapshots awaiting application. The extension matches the current
    tab's host against `host` to find the snapshot for the page it is on."""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT s.id, s.job_id, s.apply_url, s.tier, j.company, j.ats
               FROM application_snapshots s JOIN jobs j ON j.id = s.job_id
               WHERE s.status = 'draft'
               ORDER BY s.created_at, s.id"""
        ).fetchall()
        out = []
        for r in rows:
            url = r["apply_url"] or ""
            out.append({
                "snapshot_id": r["id"],
                "job_id": r["job_id"],
                "company": r["company"],
                "apply_url": url,
                "host": urlparse(url).netloc.replace("www.", ""),
                "tier": r["tier"],
                "ats": r["ats"],
            })
        return out
    finally:
        conn.close()


@app.post("/fill-plan", dependencies=[Depends(require_token)])
def fill_plan(req: FillPlanRequest):
    """Snapshot-free fact fill: the extension sends the fields it live-extracted
    from *any* page, we map each to a profile fact (job-independent, so no
    job_id/snapshot needed) and return a plan. Open/job-specific fields have no
    fact and come back `unmatched` (left blank for the human — never invented).

    Matching runs server-side (single source of truth): match_field for facts,
    is_never_fill to leave sensitive fields blank+flagged, is_auto_consent for
    tickable consents. resolve_date turns date-picker fields into a concrete date.
    """
    profile = _profile()
    fills, skipped, unmatched = [], [], []
    for f in req.fields:
        label = f.label or f.name
        ident = {"id": f.id, "label": f.label, "name": f.name}
        if profile.is_never_fill(label):
            skipped.append(ident)                     # blank + flag, by policy
            continue
        if f.type in ("checkbox", "radio") and profile.is_auto_consent(label):
            fills.append({**ident, "action": "check", "value": True,
                          "source": "profile:consent", "needs_review": False})
            continue
        match = profile.match_field(label)
        if match is None and f.name:
            match = profile.match_field(f.name)       # fall back to the input name
        if match is None:
            unmatched.append(ident)                   # no fact → leave blank
            continue
        value = match.resolve_date() or match.value   # date fields → concrete date
        needs_review = False
        if f.type == "select":
            value, needs_review = _resolve_option(value, f.options)
            action = "select_option"
        else:
            action = "fill"
        fills.append({**ident, "action": action, "value": value,
                      "source": f"profile:{match.key}", "needs_review": needs_review})
    return {"fills": fills, "skipped_never_fill": skipped, "unmatched": unmatched}


@app.get("/snapshot/{snapshot_id}", dependencies=[Depends(require_token)])
def snapshot(snapshot_id: int):
    """The fill plan for one draft: form_payload + custom_qa + cover_letter."""
    conn = _conn()
    try:
        snap = get_snapshot(conn, snapshot_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        conn.close()
    return {
        "snapshot_id": snapshot_id,
        "apply_url": snap.get("apply_url"),
        "tier": snap.get("tier"),
        "form_payload": snap.get("form_payload") or {},
        "custom_qa": snap.get("custom_qa") or [],
        "cover_letter": snap.get("cover_letter") or "",
        "verifier_report": snap.get("verifier_report") or {},
    }


def _cv_response() -> Response:
    path = _cv_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"cv not found: {path}")
    return Response(
        content=path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@app.get("/cv", dependencies=[Depends(require_token)])
def profile_cv():
    """The CV bytes, snapshot-free — one CV per candidate, so the profile-fill
    mode (any page, no draft) can attach it to resume/CV file inputs too."""
    return _cv_response()


@app.get("/snapshot/{snapshot_id}/cv", dependencies=[Depends(require_token)])
def snapshot_cv(snapshot_id: int):
    """The CV bytes to drop into the form's file input."""
    return _cv_response()


@app.post("/snapshot/{snapshot_id}/submitted", dependencies=[Depends(require_token)])
def submitted(snapshot_id: int):
    """Confirmation seen on the real site → draft→submitted, job applied,
    same-company sibling drafts abandoned. Idempotency: a second call on an
    already-submitted snapshot is an illegal transition → 409."""
    conn = _conn()
    try:
        abandoned = mark_submitted(conn, snapshot_id, note="submitted via extension")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    finally:
        conn.close()
    return {"ok": True, "snapshot_id": snapshot_id, "abandoned_siblings": abandoned}
