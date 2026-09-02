"""utils/source_health.py — warn when a scraper source goes silent.

Why: wearedevelopers' private API started answering 200 + [] on 2026-08-17.
The daily summary then read "wearedevelopers 新增 0 筆，略過 0 筆" for 16 runs
and nobody noticed that the source behind 6 of 9 first interviews had died —
a healthy source always at least *skips* known postings. So "0 added AND 0
skipped" is the signature of a dead or changed endpoint, and it must escalate
to a WARNING once it repeats, instead of hiding in an INFO line that looks
like every other day.

State lives in app_state (key `source_silent_runs:<source>`), so the counter
survives across daily runs and container rebuilds.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SILENT_RUNS_WARN = 3
_KEY = "source_silent_runs:{}"


def record_source_results(conn: sqlite3.Connection,
                          results: dict[str, tuple[int, int]],
                          warn_after: int = SILENT_RUNS_WARN) -> list[tuple[str, int]]:
    """Update each source's consecutive-silent-run counter and return the
    sources at or past the warning threshold as (source, runs)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    silent: list[tuple[str, int]] = []
    for source, (added, skipped) in results.items():
        key = _KEY.format(source)
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        prev = int(row[0]) if row and str(row[0]).isdigit() else 0
        runs = 0 if (added or skipped) else prev + 1
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, str(runs), now),
        )
        if runs >= warn_after:
            silent.append((source, runs))
    conn.commit()
    return silent


def warn_silent_sources(conn: sqlite3.Connection,
                        results: dict[str, tuple[int, int]]) -> list[tuple[str, int]]:
    """record_source_results + one WARNING per silent source."""
    silent = record_source_results(conn, results)
    for source, runs in silent:
        log.warning(
            "⚠️  source %s: %d consecutive runs with 0 added / 0 skipped — a live "
            "source always skips known postings, so its endpoint is probably dead "
            "or changed (wearedevelopers went silent for 16 days on 2026-08-17 "
            "before anyone noticed). Probe it by hand.",
            source, runs,
        )
    return silent
