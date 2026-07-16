"""draft_liveness.py — re-verify that pending drafts still point to a live form.

Drafts are generated once and then wait in the review queue; the posting can go
down while they wait (we saw 45/46 drafts sit >7 days unchecked). This sweep
re-checks each pending draft and:

  * clear-dead  -> abandon the snapshot + expire the job (frees an inventory slot)
  * suspicious  -> flag it (snapshot.liveness='suspicious') for the reviewer; keep
  * live        -> refresh the job's ats_checked_at; mark snapshot.liveness='live'

Two stages (cost vs reliability, per user decision):
  0. jobs.ats == 'gone' (ats_scan saw the source listing 404) = clear-dead with
     no request at all — catches Tier-3 drafts whose apply_url is a generic
     careers page that always loads (the zombie-draft blind spot).
  A. cheap HTTP GET — 404/410, a deep path that redirected to the bare host,
     or a 200 whose visible text says the posting is over (soft-gone) =
     clear-dead, no browser needed. A same-board redirect onto a listing
     page (slug dropped) is flagged suspicious instead — almost surely taken
     down, but a redirect is not a takedown notice.
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
from utils.gone_text import redirect_off_posting, soft_gone  # noqa: E402
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
    # the note is the reviewer-facing evidence ("redirected off the posting:
    # …") — overwritten each sweep so it always reflects the latest verdict
    conn.execute("UPDATE application_snapshots SET liveness = ?, liveness_note = ?"
                 " WHERE id = ?", (liveness, note or None, snapshot_id))
    conn.commit()
    return liveness


# ── orchestration ────────────────────────────────────────────────────────────
def _default_http_get(url: str):
    """(status, final_url, body) for a URL, following redirects.
    Errors -> (None, None, None). Test fakes may return bare 2-tuples."""
    import requests
    try:
        r = requests.get(url, timeout=12, allow_redirects=True,
                         headers={"User-Agent": _UA})
        return r.status_code, r.url, r.text[:200_000]
    except Exception:
        return None, None, None


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
        "SELECT s.id AS sid, s.job_id, s.apply_url, s.form_payload, j.ats "
        "FROM application_snapshots s JOIN jobs j ON j.id = s.job_id "
        "WHERE s.status = 'draft'")]
    tally = {"checked": 0, "live": 0, "dead": 0, "suspicious": 0}
    needs_headless = []

    def record(d, liveness, note):
        tally["checked"] += 1
        tally[liveness] += 1
        if not dry_run:
            apply_result(conn, d["sid"], d["job_id"], liveness, now, note)

    for d in drafts:
        # ats_scan checks the SOURCE listing. A 'gone' there is evidence the
        # posting is dead, BUT job boards (heise, krankenhaus-stellen) expire
        # their listing on their own schedule while the role — and the apply
        # page — stay open, and a captcha/anti-bot wall reads as 404-grade too.
        # A background sweep must never silently abandon a draft the human may
        # be mid-application on (once abandoned it can't even be marked
        # submitted). So flag it suspicious — a loud warning in the review
        # queue — and keep the draft; the human decides whether to apply.
        if d.get("ats") == "gone":
            record(d, "suspicious", "ats_scan: source listing gone")
            continue
        url = d.get("apply_url")
        if not url:
            record(d, "suspicious", "no apply_url")
            continue
        res = http_get(url)
        status, final_url = res[0], res[1]
        body = res[2] if len(res) > 2 else None
        # A 200 whose visible text says "position filled" is as dead as a 404 —
        # this was the manual Tier-3 blind spot ("page loads" passed as live
        # while the reviewer found a closed posting; 2026-07-08 review).
        gone_phrase = soft_gone(body)
        if classify_http(url, status, final_url) == "dead":
            record(d, "dead", f"http {status}")
        elif gone_phrase:
            record(d, "dead", f"soft-gone: {gone_phrase[:60]}")
        elif redirect_off_posting(url, final_url):
            # landed on a listing/other page of the same board — almost surely
            # taken down, but a redirect is not a takedown notice: flag, keep
            record(d, "suspicious", f"redirected off the posting: {final_url}")
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
