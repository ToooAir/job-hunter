"""apply_session.py — Stage 2 submission session (Step 5.3, host-only).

Attaches to the user's real Chrome (dedicated apply profile, CDP on
127.0.0.1:9222) and works through approved snapshots: re-check dedup, open
a tab, answer the cookie wall with "necessary only", execute the approved
form_payload, screenshot, and decide what to book back via snapshot_io.

Three submit gates — ALL must hold before anything is sent:
  1. snapshot status == 'approved' (a human reviewed the content)
  2. every action executed cleanly and no captcha on the live page
  3. the --submit flag; without it this is PREPARE mode: fill, screenshot,
     leave the tab open for the human to press the button

Watch mode (--watch): bookkeeping for Tier 3 manual applications. Monitors
every tab in the browser context; when a confirmation page shows up it
books the matching snapshot as submitted_by='human' — nobody has to
remember to flip the job to applied.

Red lines: no captcha bypass (captcha blocks gate 2), never "Alle
akzeptieren" on cookie walls, Tier 3 boards are never auto-submitted
(they are never 'approved' in the first place).

Usage (host venv):
  python apply_session.py                # prepare mode over approved drafts
  python apply_session.py --submit      # auto-submit when all gates pass
  python apply_session.py --watch       # monitor manual Tier 3 submissions
"""

from __future__ import annotations

import argparse
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
SCREENSHOT_DIR = ROOT / "data" / "screenshots"

PACING_RANGE_S = (30, 90)       # human-speed gap between jobs
WATCH_POLL_S = 4

# Thank-you signals (German + English). URL change alone is NOT enough —
# multi-step wizards change URLs too; text is the signal, the terminal
# y/n prompt is the fallback.
CONFIRMATION_PATTERNS = [
    r"vielen\s+dank\s+für\s+(?:ihre|deine)\s+bewerbung",
    r"danke\s+für\s+(?:ihre|deine)\s+bewerbung",
    r"bewerbung\s+(?:ist|wurde)\s+(?:erfolgreich\s+)?(?:versendet|übermittelt|eingegangen)",
    r"erfolgreich\s+(?:beworben|versendet|übermittelt)",
    r"thank\s+you\s+for\s+(?:your\s+)?applying|thank\s+you\s+for\s+your\s+application",
    r"application\s+(?:has\s+been\s+|was\s+)?(?:received|submitted|sent)",
    r"successfully\s+(?:applied|submitted)",
    r"we\s+have\s+received\s+your\s+application",
]

SUBMIT_BUTTON_PATTERNS = [
    r"bewerbung\s+(?:ab)?senden",
    r"jetzt\s+(?:ab)?senden",
    r"^\s*absenden\s*$",
    r"send\s+(?:my\s+)?application",
    r"submit\s+(?:my\s+)?application",
    r"^\s*submit\s*$",
    r"^\s*senden\s*$",
]


# ── pure logic (host-testable without a browser) ───────────────────────────────

def text_confirms(text: str) -> bool:
    low = " ".join((text or "").split()).lower()
    return any(re.search(p, low) for p in CONFIRMATION_PATTERNS)


def submit_gates(snap: dict, exec_summary: dict, captcha: bool,
                 submit_flag: bool) -> tuple[bool, list[str]]:
    """Gate check. Returns (may_submit, blocking_reasons)."""
    reasons = []
    if snap.get("status") != "approved":
        reasons.append(f"status is {snap.get('status')!r}, not approved")
    if exec_summary.get("failed"):
        reasons.append(f"{exec_summary['failed']} action(s) failed")
    if captcha:
        reasons.append("captcha on live page")
    if not submit_flag:
        reasons.append("prepare mode (no --submit)")
    return (not reasons, reasons)


def session_dedup_reason(conn, snap: dict) -> str | None:
    """Re-check at execution time: did this company enter the pipeline
    after the draft was approved? (Queue-time dedup already ran.)"""
    from utils.apply_queue import PIPELINE_STATUSES, normalize_company
    norm = normalize_company(snap["job"].get("company") or "")
    if not norm:
        return None
    placeholders = ",".join("?" for _ in PIPELINE_STATUSES)
    for row in conn.execute(
        f"SELECT id, company, status FROM jobs WHERE status IN ({placeholders})",
        PIPELINE_STATUSES,
    ):
        if row["id"] != snap["job_id"] and normalize_company(row["company"]) == norm:
            return f"company already in pipeline via job {row['id']} ({row['status']})"
    return None


def snapshot_hosts(snap: dict) -> set[str]:
    """Hosts a confirmation page for this snapshot could appear on."""
    hosts = set()
    for url in (snap.get("apply_url"), snap.get("job", {}).get("url")):
        host = urlparse(url or "").netloc.lower()
        if host:
            hosts.add(host.removeprefix("www."))
    return hosts


def match_watched_snapshot(page_host: str, watched: list[dict]) -> dict | None:
    page_host = (page_host or "").lower().removeprefix("www.")
    for snap in watched:
        if page_host and page_host in snapshot_hosts(snap):
            return snap
    return None


# ── browser helpers ────────────────────────────────────────────────────────────

def find_submit_control(page):
    from utils.browser import CLICK_TIMEOUT_MS
    for pattern in SUBMIT_BUTTON_PATTERNS:
        rx = re.compile(pattern, re.IGNORECASE)
        for finder in (
            lambda: page.get_by_role("button", name=rx),
            lambda: page.locator(
                "button, input[type=submit], [role=button]").filter(has_text=rx),
        ):
            try:
                loc = finder().first
                if loc.is_visible(timeout=CLICK_TIMEOUT_MS):
                    return loc, pattern
            except Exception:
                continue
    try:  # structural fallback: the form's own submit control
        loc = page.locator(
            "form button[type=submit], form input[type=submit]").first
        if loc.is_visible(timeout=CLICK_TIMEOUT_MS):
            return loc, "form [type=submit]"
    except Exception:
        pass
    return None, None


def page_confirms(page) -> bool:
    try:
        return text_confirms(page.locator("body").inner_text(timeout=3_000))
    except Exception:
        return False


def take_screenshot(page, snapshot_id: int, suffix: str = "") -> str | None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{snapshot_id}{suffix}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception as exc:
        print(f"    截圖失敗:{exc}")
        return None
    try:
        return str(path.relative_to(ROOT))
    except ValueError:  # screenshot dir moved outside the repo (tests)
        return str(path)


# ── per-snapshot flow ──────────────────────────────────────────────────────────

def process_snapshot(conn, context, snap: dict, submit_flag: bool) -> str:
    """Run one approved snapshot. Returns the booked outcome
    ('submitted' | 'prepared' | 'failed' | 'skipped')."""
    from utils.browser import _settle, detect_captcha, dismiss_cookie_banner
    from utils.form_executor import execute_actions
    from utils.snapshot_io import report_result

    job = snap["job"]
    payload = snap.get("form_payload") or {}
    actions = [a for a in (payload.get("actions") or [])
               if a.get("action") != "skip"]

    dedup = session_dedup_reason(conn, snap)
    if dedup:
        print(f"    跳過(dedup):{dedup}")
        report_result(conn, snap["id"], "prepared",
                      note=f"session skip — {dedup}")
        return "skipped"

    page = context.new_page()
    keep_tab_open = False
    try:
        page.goto(snap["apply_url"], wait_until="domcontentloaded")
        _settle(page)
        dismiss_cookie_banner(page)

        summary = execute_actions(page, actions)
        if summary["failed"]:
            from utils.drift_recovery import recover_and_retry
            summary = recover_and_retry(page, actions, summary)
        captcha = detect_captcha(page)
        shot = take_screenshot(page, snap["id"])
        failures = [r for r in summary["results"] if not r["ok"]]
        for r in failures:
            print(f"    ✗ {r['label'] or r['selector']}: {r['error']}")

        if summary.get("give_up"):
            report_result(conn, snap["id"], "failed", screenshot_path=shot,
                          note=f"drift: {summary['give_up']}")
            print(f"    ✗ 退回重生成:{summary['give_up']}")
            return "failed"

        ok, reasons = submit_gates(snap, summary, captcha, submit_flag)
        if not ok:
            keep_tab_open = True  # human finishes in this tab
            report_result(conn, snap["id"], "prepared", screenshot_path=shot,
                          note="prepared: " + "; ".join(reasons))
            print(f"    ⏸ prepare 模式留人按:{'; '.join(reasons)}")
            return "prepared"

        return _submit_and_book(conn, page, snap, shot)
    except Exception as exc:
        from utils.snapshot_io import report_result as _report
        _report(conn, snap["id"], "failed",
                note=f"session error: {type(exc).__name__}: {exc}"[:300])
        print(f"    ✗ 執行例外:{type(exc).__name__}: {exc}")
        return "failed"
    finally:
        if not keep_tab_open:
            try:
                page.close()
            except Exception:
                pass


def _submit_and_book(conn, page, snap: dict, shot: str | None) -> str:
    from utils.browser import _settle
    from utils.snapshot_io import report_result

    loc, pattern = find_submit_control(page)
    if loc is None:
        report_result(conn, snap["id"], "prepared", screenshot_path=shot,
                      note="prepared: no submit control found")
        print("    ⏸ 找不到送出按鈕,留人按")
        return "prepared"

    loc.click()
    _settle(page)
    confirmed = _await_confirmation(page)
    if not confirmed:
        # no thank-you text — wizard step 2, an unrecognised confirmation,
        # or a rejected submit: the human decides
        confirmed = _ask_human(snap)

    shot2 = take_screenshot(page, snap["id"], suffix="-submitted") or shot
    if confirmed:
        report_result(conn, snap["id"], "submitted", screenshot_path=shot2,
                      submitted_by="agent", note=f"submit via {pattern}")
        print("    ✓ 已送出並記錄 applied")
        return "submitted"
    report_result(conn, snap["id"], "failed", screenshot_path=shot2,
                  note="submit clicked but no confirmation detected")
    print("    ✗ 已點送出但無法確認,標 failed 留待重生成")
    return "failed"


def _await_confirmation(page, timeout_s: float = 8.0) -> bool:
    """Poll for thank-you text: submit clicks race the navigation, and many
    confirmation pages render asynchronously."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if page_confirms(page):
            return True
        try:
            page.wait_for_timeout(500)
        except Exception:
            return False
    return False


def _ask_human(snap: dict) -> bool:
    try:
        answer = input(
            f"    無法自動確認 {snap['job'].get('company')} 是否送出成功,"
            f"請看瀏覽器分頁後回答 [y/n]: ").strip().lower()
    except EOFError:  # non-interactive run — never guess 'yes'
        return False
    return answer in ("y", "yes")


# ── session drivers ────────────────────────────────────────────────────────────

def run_session(args) -> None:
    from utils.browser import cdp_session
    from utils.db import init_db
    from utils.snapshot_io import fetch_work

    conn = init_db(args.db)
    work = fetch_work(conn)
    if args.limit:
        work = work[: args.limit]
    if not work:
        print("沒有已批准的草稿。先在 dashboard 審閱頁批准。")
        return

    mode = "SUBMIT" if args.submit else "PREPARE"
    print(f"Session 開始:{len(work)} 筆已批准草稿({mode} 模式)")
    tally: dict[str, int] = {}
    companies_done: set[str] = set()
    from utils.apply_queue import normalize_company

    with cdp_session(args.endpoint or None) as context:
        first = True
        for snap in work:
            job = snap["job"]
            norm = normalize_company(job.get("company") or "")
            print(f"  [{snap['id']}] {job.get('company')} — {job.get('title')}")
            if norm in companies_done:
                print("    跳過:同公司本 session 已處理一筆")
                tally["skipped"] = tally.get("skipped", 0) + 1
                continue
            if not first:
                pause = random.uniform(*PACING_RANGE_S)
                print(f"    (等待 {pause:.0f}s 人速間隔)")
                time.sleep(pause)
            first = False
            outcome = process_snapshot(conn, context, snap, args.submit)
            tally[outcome] = tally.get(outcome, 0) + 1
            if outcome != "skipped":
                companies_done.add(norm)

    print("\n=== Session 對帳 ===")
    for key in ("submitted", "prepared", "failed", "skipped"):
        if tally.get(key):
            print(f"  {key}: {tally[key]}")


def run_watch(args) -> None:
    """Monitor the whole browser context for manual submissions (Tier 3)."""
    from utils.browser import cdp_session
    from utils.db import init_db
    from utils.snapshot_io import fetch_work, report_result

    conn = init_db(args.db)
    watched = fetch_work(conn, status="draft") + fetch_work(conn, status="approved")
    if not watched:
        print("沒有可監看的 snapshot(draft/approved 皆空)。")
        return
    print(f"Watch 模式:監看 {len(watched)} 筆 snapshot 的確認頁,Ctrl+C 結束")
    for snap in watched:
        print(f"  [{snap['id']}] {snap['job'].get('company')} — "
              f"{', '.join(sorted(snapshot_hosts(snap))) or '(無 host)'}")

    seen_urls: dict[int, str] = {}
    booked: set[int] = set()
    warned: set[str] = set()
    try:
        with cdp_session(args.endpoint or None) as context:
            while len(booked) < len(watched):
                for page in list(context.pages):
                    try:
                        url = page.url
                    except Exception:
                        continue
                    key = id(page)
                    url_changed = seen_urls.get(key) != url
                    seen_urls[key] = url
                    host = urlparse(url).netloc
                    snap = match_watched_snapshot(host, watched)
                    if snap is None:
                        if not url_changed:
                            continue
                        # Tier 3 snapshots carry the board posting's host,
                        # but the real apply flow often lives elsewhere
                        # (e.g. join.com) — surface the confirmation
                        # instead of dropping it silently.
                        if url not in warned and page_confirms(page):
                            warned.add(url)
                            print(f"  ⚠ 確認頁對不到監看中的 snapshot：{url}")
                            print("    （若這是你剛送出的申請，用"
                                  " --book <snapshot_id> 手動入帳）")
                        continue
                    if snap["id"] in booked:
                        continue
                    # matched pages are re-checked EVERY poll even with an
                    # unchanged URL — inline (AJAX) submissions like
                    # arbeitnow's swap in the success text without navigating
                    if not page_confirms(page):
                        if url_changed and url not in warned:
                            warned.add(url)
                            print(f"  · 看見 [{snap['id']}] "
                                  f"{snap['job'].get('company')} 的頁面，"
                                  f"等待確認文字…")
                        continue
                    shot = take_screenshot(page, snap["id"], suffix="-confirmed")
                    report_result(conn, snap["id"], "submitted",
                                  screenshot_path=shot, submitted_by="human",
                                  note=f"watch: confirmation on {url}"[:300])
                    booked.add(snap["id"])
                    print(f"  ✓ [{snap['id']}] {snap['job'].get('company')} "
                          f"確認頁偵測到,已記錄 applied")
                time.sleep(WATCH_POLL_S)
    except KeyboardInterrupt:
        pass
    print(f"\nWatch 結束:記錄 {len(booked)} 筆 applied。")


def run_book(args) -> None:
    """Manually book a human submission watch couldn't attribute by host."""
    from utils.db import init_db
    from utils.snapshot_io import report_result

    conn = init_db(args.db)
    snap = conn.execute(
        "SELECT s.id, s.status, j.company, j.title FROM application_snapshots s"
        " JOIN jobs j ON j.id = s.job_id WHERE s.id = ?", (args.book,)).fetchone()
    if snap is None:
        print(f"找不到 snapshot {args.book}。")
        return
    report_result(conn, args.book, "submitted",
                  note="booked manually via --book "
                       "(human submission outside watched hosts)",
                  submitted_by="human")
    print(f"已記錄 [{snap['id']}] {snap['company']} — {snap['title']}"
          f" 為人工送出,職缺轉 applied。")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 2 submission session (host)")
    ap.add_argument("--db", default=str(ROOT / "data" / "jobs.db"))
    ap.add_argument("--endpoint", default=None,
                    help="CDP endpoint (default http://127.0.0.1:9222)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--submit", action="store_true",
                    help="auto-submit when all gates pass (default: prepare)")
    ap.add_argument("--watch", action="store_true",
                    help="monitor manual Tier 3 submissions instead")
    ap.add_argument("--book", type=int, metavar="SNAPSHOT_ID",
                    help="record a manual submission watch missed (no browser)")
    args = ap.parse_args()
    if args.book:
        run_book(args)
    elif args.watch:
        run_watch(args)
    else:
        run_session(args)


if __name__ == "__main__":
    main()
