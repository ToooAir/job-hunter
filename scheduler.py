"""
Catch-up pipeline scheduler — runs inside Docker container.

Interval-based instead of clock-based: the pipeline runs as soon as BOTH hold
  * the last finished run (success or failed) ended >= MIN_INTERVAL_HOURS ago
  * the machine is online (HTTPS probe)
so a laptop that is asleep at any fixed hour still catches up the next time it
is awake. Progress is tracked per stage in the pipeline_runs table: a run
interrupted by sleep/offline/crash resumes from the first unfinished stage.

A stage exiting with code 75 (EX_TEMPFAIL, see phase2_scorer.EXIT_TRANSIENT)
signals a transient network failure: the run stays open and is retried with
exponential backoff. Any other non-zero exit marks the run failed and the
next attempt waits the normal interval.
"""

import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from utils.db import (
    auto_expire_stale_jobs,
    auto_ghost_stale_applications,
    finish_pipeline_run,
    get_last_pipeline_completed_at,
    get_open_pipeline_run,
    init_db,
    mark_pipeline_stage_done,
    start_pipeline_run,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH            = os.getenv("DB_PATH", "./data/jobs.db")
MIN_INTERVAL_HOURS = float(os.getenv("PIPELINE_MIN_INTERVAL_HOURS", "20"))
TICK_SECONDS       = int(os.getenv("PIPELINE_TICK_SECONDS", "60"))
PROBE_URL          = os.getenv("PIPELINE_PROBE_URL", "https://api.mistral.ai/")
STALE_RUN_HOURS    = 48   # an open run older than this is abandoned, not resumed
EX_TEMPFAIL        = 75   # contract with phase2_scorer.EXIT_TRANSIENT
BACKOFF_BASE_S     = 300  # first retry after a transient failure: 5 min
BACKOFF_MAX_S      = 3600

STAGES = ("phase1_ingestor.py", "phase2_scorer.py")

LOG_FILE = Path(__file__).parent / "logs" / "pipeline.log"


# Lines matching any of these substrings are echoed to docker logs.
# Everything is still written to pipeline.log regardless.
#
# Phase 1 patterns:  === (start/end), 新增 (per-source + total summary), [WARNING]/[ERROR]
# Phase 2 patterns:  LLM provider (model info), 評分 (job counts), 完成： (scored/failed),
#                    級： (A/B/C breakdown), en_required (lang breakdown), [WARNING]/[ERROR]
_DOCKER_LOG_PATTERNS = (
    "[WARNING]",
    "[ERROR]",
    "===",           # phase start/end markers
    "scraping ",     # phase1 per-source start indicator (e.g. "scraping wearedevelopers …")
    "新增",          # phase1 per-source + total summary (新增 X 筆，略過 Y 筆)
    "LLM provider",  # phase2 startup — which model is running
    "評分",          # phase2: 開始評分 N 筆 / 完成評分 / 沒有待評分
    "完成：",        # phase2: 完成：N 筆成功，M 筆失敗 (note: colon avoids false matches)
    "級：",          # phase2: A 級/B 級/C 級 breakdown
    "en_required",   # phase2: language breakdown line
)


def _stream_phase(script: str, log_file) -> int:
    """
    Run a phase script, writing all output to log_file and echoing
    only the important summary/error lines to stdout (→ docker logs).
    Returns the exit code.
    """
    proc = subprocess.Popen(
        [sys.executable, "-u", script],  # -u: unbuffered → real-time lines
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,         # merge stderr into stdout
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        log_file.write(line)
        log_file.flush()
        if any(pat in line for pat in _DOCKER_LOG_PATTERNS):
            print(line, end="", flush=True)
    proc.wait()
    return proc.returncode


def is_online(url: str = PROBE_URL, timeout: float = 5.0) -> bool:
    """HTTPS reachability probe.

    Any real HTTP response (even 4xx) counts as online. Captive portals
    fail the TLS handshake against the pinned hostname, so they read as
    offline — which is what we want.
    """
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def run_is_due(last_completed_at: str | None, now: datetime, min_interval_hours: float) -> bool:
    """True when enough time has passed since the last finished run."""
    if last_completed_at is None:
        return True
    try:
        last = datetime.fromisoformat(last_completed_at)
    except ValueError:
        return True
    return (now - last) >= timedelta(hours=min_interval_hours)


def _housekeeping(conn) -> None:
    try:
        n_expired = auto_expire_stale_jobs(conn)
        if n_expired:
            log.info("expired %d stale job(s) (TTL or expires_at exceeded)", n_expired)
        n_ghosted = auto_ghost_stale_applications(conn)
        if n_ghosted:
            log.info("ghosted %d stale application(s) (no response in 35 days)", n_ghosted)
    except Exception as exc:
        log.error("housekeeping failed: %s", exc)


class Scheduler:
    def __init__(self) -> None:
        self.tempfail_count = 0
        self.next_attempt_at: datetime | None = None
        self.offline_logged = False

    def _backoff(self) -> None:
        self.tempfail_count += 1
        delay = min(BACKOFF_BASE_S * 2 ** (self.tempfail_count - 1), BACKOFF_MAX_S)
        self.next_attempt_at = datetime.now() + timedelta(seconds=delay)
        log.warning(
            "transient failure #%d — next attempt at %s",
            self.tempfail_count, self.next_attempt_at.strftime("%H:%M:%S"),
        )

    def _reset_backoff(self) -> None:
        self.tempfail_count = 0
        self.next_attempt_at = None

    def tick(self) -> None:
        now = datetime.now()
        if self.next_attempt_at and now < self.next_attempt_at:
            return

        conn = init_db(DB_PATH)
        try:
            run = get_open_pipeline_run(conn)

            # Abandon a run that has been open for days (e.g. container was down)
            # — its phase-1 data would be stale by the time it finished.
            if run is not None:
                started = datetime.fromisoformat(run["started_at"])
                if now - started > timedelta(hours=STALE_RUN_HOURS):
                    finish_pipeline_run(conn, run["id"], "failed", "stale run abandoned")
                    log.warning("abandoned stale run #%d (started %s)", run["id"], run["started_at"])
                    run = None

            if run is None:
                if not run_is_due(get_last_pipeline_completed_at(conn), now, MIN_INTERVAL_HOURS):
                    return
                if not self._check_online():
                    return
                run_id = start_pipeline_run(conn)
                run = get_open_pipeline_run(conn)
                log.info("════ Pipeline run #%d start ════", run_id)
            else:
                if not self._check_online():
                    return
                log.info("════ Pipeline run #%d resume (done: %s) ════",
                         run["id"], run["stages_done"] or "none")

            self._execute_run(conn, run)
        finally:
            conn.close()

    def _check_online(self) -> bool:
        if is_online():
            if self.offline_logged:
                log.info("back online")
                self.offline_logged = False
            return True
        if not self.offline_logged:
            log.info("offline — pipeline run deferred until connectivity returns")
            self.offline_logged = True
        return False

    def _execute_run(self, conn, run: dict) -> None:
        done = {s for s in (run["stages_done"] or "").split(",") if s}
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a") as lf:
            for stage in STAGES:
                if stage in done:
                    continue
                log.info("▶ %s", stage)
                rc = _stream_phase(stage, lf)
                if rc == EX_TEMPFAIL:
                    self._backoff()  # run stays open → resumed next eligible tick
                    return
                if rc != 0:
                    finish_pipeline_run(conn, run["id"], "failed", f"{stage} exited {rc}")
                    self._reset_backoff()
                    log.error(
                        "%s exited with code %d — run #%d failed; next attempt in %sh",
                        stage, rc, run["id"], MIN_INTERVAL_HOURS,
                    )
                    return
                mark_pipeline_stage_done(conn, run["id"], stage)
        _housekeeping(conn)
        finish_pipeline_run(conn, run["id"], "success")
        self._reset_backoff()
        log.info("════ Pipeline run #%d done ════", run["id"])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        log.warning(
            "clock arguments %s are deprecated — schedule is interval-based now "
            "(PIPELINE_MIN_INTERVAL_HOURS=%.0fh)", sys.argv[1:], MIN_INTERVAL_HOURS,
        )
    log.info(
        "Catch-up scheduler ready — min interval %.0fh, tick %ds, probe %s",
        MIN_INTERVAL_HOURS, TICK_SECONDS, PROBE_URL,
    )
    log.info("Container time: %s", datetime.now().isoformat())

    scheduler = Scheduler()
    while True:
        try:
            scheduler.tick()
        except Exception as exc:
            log.error("Scheduler tick error: %s", exc)
        time.sleep(TICK_SECONDS)
