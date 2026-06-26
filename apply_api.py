#!/usr/bin/env python3
"""apply_api.py — the local sidecar the autofill extension talks to.

Phase 1 (extension/PHASE1_PLAN.md): a token-gated HTTP API on host-loopback that
lets the browser extension pull the right reviewed draft for the page it is on,
fetch the CV bytes to upload, and mark the application submitted on confirmation.

Reuses utils.snapshot_io / utils.db — no new data model. Read-only except the
single `submitted` POST, which only advances the existing draft→submitted
lifecycle transition (and books the job applied + abandons same-company siblings,
via mark_submitted).

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


@app.get("/snapshot/{snapshot_id}/cv", dependencies=[Depends(require_token)])
def snapshot_cv(snapshot_id: int):
    """The CV bytes to drop into the form's file input."""
    path = _cv_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"cv not found: {path}")
    return Response(
        content=path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


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
