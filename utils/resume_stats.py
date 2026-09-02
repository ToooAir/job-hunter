"""resume_stats.py — measure how effective the applications actually are.

Pure Python over the jobs table; no LLM, no network. This is the measurement
side of the résumé-conversion experiment: apply only to high-fit, extension-
fillable jobs (see apply_queue APPLY_MIN_SCORE / APPLY_ADDRESSABLE_ONLY), then
watch whether the interview rate moves.

Two filters, deliberately separated — a résumé can pass one and fail the other:
  1. Got read      — any reply (rejected counts: a human looked). The opposite
                     is `ghosted`. High read-rate => targeting/ATS-passability OK.
  2. Got an interview — reached interview_1+. This is the real conversion.

`pending` (status still 'applied', no verdict yet) is held out of the read-rate
denominator so a fresh batch doesn't masquerade as a black hole.

    python -m utils.resume_stats [--since 2026-07-01] [--db PATH]
"""

import argparse
import re
import statistics
from datetime import date
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.apply_queue import DEFAULT_DB_PATH  # noqa: E402
from utils.db import init_db  # noqa: E402

# peak_stage values that mean "reached a real conversation or better".
INTERVIEW_STAGES = ("interview_1", "interview_2", "offer")


def _rate(num: int, den: int) -> float | None:
    return round(100 * num / den, 1) if den else None


def _bucket(rows: list[dict]) -> dict:
    """Classify a set of applied jobs into the two-filter funnel."""
    applied = len(rows)
    interview = sum(1 for r in rows if (r["peak_stage"] or "") in INTERVIEW_STAGES)
    offer = sum(1 for r in rows if (r["peak_stage"] or "") == "offer")
    ghosted = sum(1 for r in rows if r["status"] == "ghosted")
    # 'applied' status with no interview reached = still open, verdict unknown.
    pending = sum(1 for r in rows
                  if r["status"] == "applied"
                  and (r["peak_stage"] or "") not in INTERVIEW_STAGES)
    responded = applied - ghosted - pending  # rejected + interview + offer
    decided = responded + ghosted            # excludes pending (unknown)
    return {
        "applied": applied,
        "responded": responded,
        "ghosted": ghosted,
        "pending": pending,
        "interview": interview,
        "offer": offer,
        "response_rate": _rate(responded, decided),   # of jobs that gave a verdict
        "interview_rate": _rate(interview, applied),   # of all applied
    }


# Rejection bookings from the extension leave "[YYYY-MM-DD…] rejected" in
# notes — the only status-change timestamp the schema has. That makes the
# knockout smoke test measurable: a rejection inside a week of applying is a
# screening-stage verdict (visa / language / résumé pattern), not a hiring
# manager's. 2026-09-02 baseline: 67% of 199 rejections ≤ 7 days, median 6.
REJECT_NOTE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})[^\]]*\] rejected")
QUICK_REJECT_DAYS = 7


def _days_to_reject(row: dict) -> int | None:
    m = REJECT_NOTE_RE.search(row.get("notes") or "")
    if not m or not row.get("applied_at"):
        return None
    try:
        applied = date.fromisoformat(row["applied_at"][:10])
        rejected = date.fromisoformat(m.group(1))
    except ValueError:
        return None
    return max(0, (rejected - applied).days)


def _reject_bucket(days: list[int]) -> dict:
    n = len(days)
    quick = sum(1 for d in days if d <= QUICK_REJECT_DAYS)
    return {
        "n": n,
        "median_days": statistics.median(days) if days else None,
        "quick": quick,
        "quick_share": _rate(quick, n),
    }


def quick_rejects(conn, since: str | None = None) -> dict:
    """Time-to-rejection for applications whose rejection carries a booking
    date. Returns {overall, by_source, by_lang, since}; groups with fewer
    than 5 datapoints are still returned — the printer hides them."""
    sql = ("SELECT source, jd_language_req, applied_at, notes FROM jobs "
           "WHERE status = 'rejected' AND applied_at IS NOT NULL")
    params: list = []
    if since:
        sql += " AND applied_at >= ?"
        params.append(since)
    rows = [dict(r) for r in conn.execute(sql, params)]
    timed = [(r, d) for r in rows if (d := _days_to_reject(r)) is not None]

    def group(key):
        keys = sorted({r[key] for r, _ in timed if r[key]})
        return {k: _reject_bucket([d for r, d in timed if r[key] == k]) for k in keys}

    return {
        "overall": _reject_bucket([d for _, d in timed]),
        "untimed": len(rows) - len(timed),
        "by_source": group("source"),
        "by_lang": group("jd_language_req"),
        "since": since,
    }


def effectiveness(conn, since: str | None = None) -> dict:
    """Résumé-effectiveness funnel for applied jobs (optionally since a date).

    Returns {overall, by_grade, by_source, since}. `since` filters on applied_at
    (ISO prefix compare) — pass the experiment start date to isolate the cohort.
    """
    sql = ("SELECT source, fit_grade, match_score, status, peak_stage, applied_at "
           "FROM jobs WHERE applied_at IS NOT NULL")
    params: list = []
    if since:
        sql += " AND applied_at >= ?"
        params.append(since)
    rows = [dict(r) for r in conn.execute(sql, params)]

    by_grade = {
        g: _bucket([r for r in rows if r["fit_grade"] == g])
        for g in sorted({r["fit_grade"] for r in rows if r["fit_grade"]})
    }
    by_source = {
        s: _bucket([r for r in rows if r["source"] == s])
        for s in sorted({r["source"] for r in rows if r["source"]})
    }
    return {
        "overall": _bucket(rows),
        "by_grade": by_grade,
        "by_source": by_source,
        "since": since,
    }


def _print(stats: dict) -> None:
    o = stats["overall"]
    scope = f"（applied_at >= {stats['since']}）" if stats["since"] else "（全部）"
    print(f"=== 履歷效果{scope} ===")
    print(f"投遞 {o['applied']}｜有回應 {o['responded']}｜已讀不回 {o['ghosted']}"
          f"｜待定 {o['pending']}｜面試 {o['interview']}｜offer {o['offer']}")
    print(f"回應率（已定案）：{o['response_rate']}%   ← 履歷被讀到了嗎")
    print(f"一面轉換率（全投遞）：{o['interview_rate']}%   ← 履歷有沒有轉成對話")
    print()
    print(f"{'grade':<6} {'投':>4} {'面試':>4} {'ghost':>5} {'一面率':>7}")
    for g, b in stats["by_grade"].items():
        print(f"{g:<6} {b['applied']:>4} {b['interview']:>4} {b['ghosted']:>5} "
              f"{(str(b['interview_rate']) + '%') if b['interview_rate'] is not None else '—':>7}")
    print()
    print("分管道（投遞>=5 才顯示）：")
    print(f"{'source':<18} {'投':>4} {'面試':>4} {'ghost':>5} {'一面率':>7}")
    for s, b in sorted(stats["by_source"].items(), key=lambda kv: -kv[1]["applied"]):
        if b["applied"] < 5:
            continue
        print(f"{s:<18} {b['applied']:>4} {b['interview']:>4} {b['ghosted']:>5} "
              f"{(str(b['interview_rate']) + '%') if b['interview_rate'] is not None else '—':>7}")


def _print_quick_rejects(stats: dict) -> None:
    o = stats["overall"]
    print()
    print(f"=== 快拒（≤{QUICK_REJECT_DAYS} 天 = 篩選層 knockout 訊號）===")
    if not o["n"]:
        print("沒有帶日期的拒信紀錄（extension ✉️ 入帳才會留時間戳）")
        return
    print(f"有時間戳的拒信 {o['n']}（無時間戳 {stats['untimed']}）｜中位 {o['median_days']} 天"
          f"｜{QUICK_REJECT_DAYS} 天內 {o['quick']}（{o['quick_share']}%）")
    for label, groups in (("分管道", stats["by_source"]), ("分語言標籤", stats["by_lang"])):
        print(f"{label}（n>=5 才顯示）：")
        print(f"{'group':<18} {'n':>4} {'中位':>5} {'快拒率':>7}")
        for k, b in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
            if b["n"] < 5:
                continue
            print(f"{k:<18} {b['n']:>4} {b['median_days']:>5} {str(b['quick_share']) + '%':>7}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Résumé-effectiveness funnel (read-only).")
    parser.add_argument("--since", default=None,
                        help="isolate the cohort applied on/after this date, e.g. 2026-07-01")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        _print(effectiveness(conn, since=args.since))
        _print_quick_rejects(quick_rejects(conn, since=args.since))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
