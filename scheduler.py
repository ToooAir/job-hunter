"""
Daily pipeline scheduler — runs inside Docker container.
Executes Phase 1 + Phase 2 every day at SCHEDULE_HOUR:SCHEDULE_MIN (local container time).
No extra dependencies — standard library only.
"""

import logging
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

SCHEDULE_HOUR = int(sys.argv[1]) if len(sys.argv) > 1 else 7
SCHEDULE_MIN  = int(sys.argv[2]) if len(sys.argv) > 2 else 30


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


def run_pipeline() -> None:
    log.info("════ Pipeline start ════")
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as lf:
        for phase in ("phase1_ingestor.py", "phase2_scorer.py"):
            log.info("▶ %s", phase)
            returncode = _stream_phase(phase, lf)
            if returncode != 0:
                log.error("%s exited with code %d — aborting pipeline", phase, returncode)
                break
    log.info("════ Pipeline done  ════")


last_run: date | None = None

log.info("Scheduler ready — will run daily at %02d:%02d", SCHEDULE_HOUR, SCHEDULE_MIN)
log.info("Container time: %s", datetime.now().isoformat())

while True:
    try:
        now = datetime.now()
        if (now.hour == SCHEDULE_HOUR
                and now.minute == SCHEDULE_MIN
                and now.date() != last_run):
            run_pipeline()
            last_run = now.date()
    except Exception as exc:
        log.error("Scheduler loop error: %s", exc)
    time.sleep(30)
