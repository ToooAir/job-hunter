"""draft_liveness.py — re-verify that pending drafts still point to a live form.

Drafts are generated once and then wait in the review queue; the posting can go
down while they wait (we saw 45/46 drafts sit >7 days unchecked). This sweep
re-checks each pending draft and:

  * clear-dead  -> abandon the snapshot + expire the job (frees an inventory slot)
  * suspicious  -> flag it (snapshot.liveness='suspicious') for the reviewer; keep
  * live        -> refresh the job's ats_checked_at; mark snapshot.liveness='live'

Two stages (cost vs reliability, per user decision):
  A. cheap HTTP GET — 404/410 or a deep path that redirected to the bare host =
     clear-dead, no browser needed.
  B. headless confirm (only the rest) — the real liveness signal is whether the
     application FORM is still there, reusing Stage 1's extraction + verdict.

Conservative: only clear-dead is auto-withdrawn; account walls / captcha / weak
forms / nav errors are 'suspicious' and merely flagged.

CLI:  python -m utils.draft_liveness [--dry-run] [--db PATH]
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import init_db, mark_expired  # noqa: E402
from utils.snapshot_io import abandon_snapshot  # noqa: E402

DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "jobs.db")
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ── pure classifiers ─────────────────────────────────────────────────────────
def _is_deep(path: str | None) -> bool:
    return bool((path or "").strip("/"))


def _redirect_to_home(orig_url: str, final_url: str | None) -> bool:
    """A deep posting URL that lands on the bare host root = taken down."""
    if not final_url:
        return False
    o, f = urlparse(orig_url), urlparse(final_url)
    return _is_deep(o.path) and not _is_deep(f.path)


def classify_http(orig_url: str, status: int | None, final_url: str | None) -> str:
    """HTTP-stage verdict: 'dead' | 'maybe' | 'unknown'.

    'maybe'/'unknown' are not decided here — they go to the headless form check.
    """
    if status in (404, 410):
        return "dead"
    if status is not None and 200 <= status < 400:
        return "dead" if _redirect_to_home(orig_url, final_url) else "maybe"
    return "unknown"  # 403 / 5xx / timeout / connection error — can't tell cheaply


_LIVE_VERDICTS = {"ok"}
_DEAD_VERDICTS = {"gone", "no-form"}


def liveness_from_verdict(verdict: str) -> str:
    """Form-presence verdict -> 'live' | 'dead' | 'suspicious'. Everything that
    isn't a clean form (ok) or a clear absence (gone/no-form) is suspicious:
    account walls, captcha, weak forms, nav errors — flagged, never auto-removed.
    Only applied to drafts that actually had a fillable form (see _has_actions).
    """
    if verdict in _LIVE_VERDICTS:
        return "live"
    if verdict in _DEAD_VERDICTS:
        return "dead"
    return "suspicious"


def _has_actions(form_payload) -> bool:
    """Did this draft have a fillable form (auto-fill actions)? Manual Tier-3
    drafts (board / account / captcha answer-sheet) never did — for them
    'no form present' is the normal state, not a liveness problem."""
    if not form_payload:
        return False
    try:
        payload = json.loads(form_payload) if isinstance(form_payload, str) else form_payload
        return bool(payload.get("actions"))
    except (ValueError, AttributeError):
        return False


# ── persistence ──────────────────────────────────────────────────────────────
def apply_result(conn, snapshot_id: int, job_id: str, liveness: str,
                 now: str | None = None, note: str = "") -> str:
    """Persist one draft's outcome. dead -> abandon + expire; live/suspicious ->
    refresh ats_checked_at and set the snapshot flag. Returns `liveness`."""
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if liveness == "dead":
        abandon_snapshot(conn, snapshot_id,
                         reason=f"liveness sweep: {note or 'posting gone'}")
        mark_expired(conn, [job_id])
        return "dead"
    conn.execute("UPDATE jobs SET ats_checked_at = ? WHERE id = ?", (now, job_id))
    conn.execute("UPDATE application_snapshots SET liveness = ? WHERE id = ?",
                 (liveness, snapshot_id))
    conn.commit()
    return liveness


# ── orchestration ────────────────────────────────────────────────────────────
def _default_http_get(url: str):
    """(status, final_url) for a URL, following redirects. Errors -> (None, None)."""
    import requests
    try:
        r = requests.get(url, timeout=12, allow_redirects=True,
                         headers={"User-Agent": _UA})
        return r.status_code, r.url
    except Exception:
        return None, None


def _headless_verdicts(drafts):
    """Yield (draft, verdict) for each draft using one headless browser session.
    Reuses Stage 1's reach + extract + verdict (lazy import: container only)."""
    from apply_stage1 import verdict_of
    from utils.browser import extract_form_tree, goto_apply_page, headless_session

    with headless_session() as ctx:
        for d in drafts:
            page = ctx.new_page()
            try:
                report = goto_apply_page(page, d["apply_url"])
                active = report.pop("page", page)
                tree = extract_form_tree(active) if report["form_found"] else None
                if active is not page:
                    active.close()
                yield d, verdict_of(report, tree)
            except Exception:
                yield d, "nav-error"
            finally:
                page.close()


def sweep_drafts(conn, http_get=None, headless_verdicts=None,
                 dry_run: bool = False, now: str | None = None) -> dict:
    """Re-verify every pending draft. Returns a tally dict."""
    http_get = http_get or _default_http_get
    drafts = [dict(r) for r in conn.execute(
        "SELECT id AS sid, job_id, apply_url, form_payload FROM application_snapshots "
        "WHERE status = 'draft'")]
    tally = {"checked": 0, "live": 0, "dead": 0, "suspicious": 0}
    needs_headless = []

    def record(d, liveness, note):
        tally["checked"] += 1
        tally[liveness] += 1
        if not dry_run:
            apply_result(conn, d["sid"], d["job_id"], liveness, now, note)

    for d in drafts:
        url = d.get("apply_url")
        if not url:
            record(d, "suspicious", "no apply_url")
            continue
        status, final_url = http_get(url)
        if classify_http(url, status, final_url) == "dead":
            record(d, "dead", f"http {status}")
        elif not _has_actions(d.get("form_payload")):
            # Manual Tier-3 draft: there was never a fillable form to lose, so the
            # page simply loading is liveness enough — don't run headless or flag it.
            record(d, "live", "page loads (manual)")
        else:
            needs_headless.append(d)

    if needs_headless:
        hv = headless_verdicts or _headless_verdicts
        for d, verdict in hv(needs_headless):
            record(d, liveness_from_verdict(verdict), f"form {verdict}")

    return tally


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-verify pending drafts' liveness.")
    parser.add_argument("--dry-run", action="store_true", help="no DB writes")
    parser.add_argument("--db", default=os.getenv("DB_PATH", DEFAULT_DB_PATH))
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        tally = sweep_drafts(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print(f"draft liveness sweep (dry_run={args.dry_run}): "
          f"checked {tally['checked']} → live {tally['live']}, "
          f"dead {tally['dead']} (withdrawn+expired), "
          f"suspicious {tally['suspicious']} (flagged)")


if __name__ == "__main__":
    main()
