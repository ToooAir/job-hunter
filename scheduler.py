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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

SCHEDULE_HOUR = int(sys.argv[1]) if len(sys.argv) > 1 else 7
SCHEDULE_MIN  = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def run_pipeline() -> None:
    log.info("════ Pipeline start ════")
    for phase in ("phase1_ingestor.py", "phase2_scorer.py"):
        log.info("Running %s", phase)
        result = subprocess.run([sys.executable, phase])
        if result.returncode != 0:
            log.error("%s exited with code %d", phase, result.returncode)
    log.info("════ Pipeline done  ════")


last_run: date | None = None

log.info("Scheduler ready — will run daily at %02d:%02d", SCHEDULE_HOUR, SCHEDULE_MIN)
log.info("Container time: %s", datetime.now().isoformat())

while True:
    now = datetime.now()
    if (now.hour == SCHEDULE_HOUR
            and now.minute == SCHEDULE_MIN
            and now.date() != last_run):
        run_pipeline()
        last_run = now.date()
    time.sleep(30)
