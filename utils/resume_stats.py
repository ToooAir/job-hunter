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


def main() -> None:
    parser = argparse.ArgumentParser(description="Résumé-effectiveness funnel (read-only).")
    parser.add_argument("--since", default=None,
                        help="isolate the cohort applied on/after this date, e.g. 2026-07-01")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        _print(effectiveness(conn, since=args.since))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
