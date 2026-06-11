"""Tests for the catch-up scheduler: pipeline_runs tracking, interval logic,
transient-error classification, and the phase-2 network-safety behavior
(network failures must keep jobs un-scored instead of marking them error).

Run:  python -m unittest discover tests -v
"""

import os
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

# Must be set before importing phase2_scorer / utils.llm (env read at import time).
# load_dotenv() does not override variables that already exist.
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402  (transitive dependency of openai)
import openai  # noqa: E402

import phase2_scorer  # noqa: E402
import scheduler  # noqa: E402
from utils.db import (  # noqa: E402
    finish_pipeline_run,
    get_last_pipeline_completed_at,
    get_open_pipeline_run,
    init_db,
    mark_pipeline_stage_done,
    start_pipeline_run,
)

LONG_EN_JD = (
    "We are looking for a backend engineer with strong Python skills. "
    "You will design and operate scalable services, work with PostgreSQL "
    "and Redis, and collaborate with a cross-functional product team. "
    "Experience with Docker and cloud platforms is a plus."
)


def _make_db(tmpdir: str):
    return init_db(os.path.join(tmpdir, "jobs.db"))


def _insert_unscored(conn, job_id: str):
    conn.execute(
        "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, "ACME GmbH", "Backend Engineer", f"https://x.test/{job_id}",
         "test", LONG_EN_JD, "2026-06-11T00:00:00"),
    )
    conn.commit()


class PipelineRunsTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_db(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_run_lifecycle(self):
        self.assertIsNone(get_open_pipeline_run(self.conn))
        self.assertIsNone(get_last_pipeline_completed_at(self.conn))

        run_id = start_pipeline_run(self.conn)
        run = get_open_pipeline_run(self.conn)
        self.assertEqual(run["id"], run_id)
        self.assertEqual(run["stages_done"], "")

        mark_pipeline_stage_done(self.conn, run_id, "phase1_ingestor.py")
        mark_pipeline_stage_done(self.conn, run_id, "phase2_scorer.py")
        run = get_open_pipeline_run(self.conn)
        self.assertEqual(run["stages_done"], "phase1_ingestor.py,phase2_scorer.py")

        finish_pipeline_run(self.conn, run_id, "success")
        self.assertIsNone(get_open_pipeline_run(self.conn))
        self.assertIsNotNone(get_last_pipeline_completed_at(self.conn))

    def test_stage_marking_is_idempotent(self):
        run_id = start_pipeline_run(self.conn)
        mark_pipeline_stage_done(self.conn, run_id, "phase1_ingestor.py")
        mark_pipeline_stage_done(self.conn, run_id, "phase1_ingestor.py")
        run = get_open_pipeline_run(self.conn)
        self.assertEqual(run["stages_done"], "phase1_ingestor.py")

    def test_failed_run_also_gates_next_attempt(self):
        run_id = start_pipeline_run(self.conn)
        finish_pipeline_run(self.conn, run_id, "failed", "phase1 exited 1")
        # A hard failure must still set completed_at so the next attempt
        # waits the normal interval instead of retrying every tick.
        self.assertIsNotNone(get_last_pipeline_completed_at(self.conn))


class RunIsDueTest(unittest.TestCase):
    def test_never_run_is_due(self):
        self.assertTrue(scheduler.run_is_due(None, datetime.now(), 20))

    def test_recent_run_is_not_due(self):
        last = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertFalse(scheduler.run_is_due(last, datetime.now(), 20))

    def test_old_run_is_due(self):
        last = (datetime.now() - timedelta(hours=21)).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertTrue(scheduler.run_is_due(last, datetime.now(), 20))

    def test_garbage_timestamp_is_due(self):
        self.assertTrue(scheduler.run_is_due("not-a-date", datetime.now(), 20))


class IsOnlineTest(unittest.TestCase):
    def test_unreachable_host_is_offline(self):
        self.assertFalse(scheduler.is_online("https://127.0.0.1:1/", timeout=1))


class TransientClassificationTest(unittest.TestCase):
    def _status_error(self, code: int):
        req = httpx.Request("POST", "https://api.test/v1/chat")
        resp = httpx.Response(code, request=req)
        return openai.APIStatusError("boom", response=resp, body=None)

    def test_connection_error_is_transient(self):
        exc = openai.APIConnectionError(request=httpx.Request("POST", "https://api.test"))
        self.assertTrue(phase2_scorer._is_transient_llm_error(exc))

    def test_5xx_is_transient(self):
        self.assertTrue(phase2_scorer._is_transient_llm_error(self._status_error(503)))

    def test_429_is_not_transient(self):
        # RateLimitError has its own sleep-and-retry path in the worker.
        self.assertFalse(phase2_scorer._is_transient_llm_error(self._status_error(429)))

    def test_content_error_is_not_transient(self):
        self.assertFalse(phase2_scorer._is_transient_llm_error(ValueError("bad json")))


class ScoreJobsNetworkSafetyTest(unittest.TestCase):
    """The high-risk change: network failures must NOT permanently mark jobs
    as error (they stay un-scored), while content failures still do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "jobs.db")
        self.qdrant_path = os.path.join(self.tmp.name, "qdrant")
        conn = init_db(self.db_path)
        for jid in ("j1", "j2", "j3"):
            _insert_unscored(conn, jid)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _statuses(self):
        conn = init_db(self.db_path)
        rows = conn.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()
        conn.close()
        return {r["id"]: r["status"] for r in rows}

    def _run_score_jobs(self):
        return phase2_scorer.score_jobs(db_path=self.db_path, qdrant_path=self.qdrant_path)

    def test_network_failure_keeps_jobs_unscored(self):
        net_err = openai.APIConnectionError(
            request=httpx.Request("POST", "https://api.test/v1/chat")
        )
        with mock.patch.object(phase2_scorer, "check_kb_ready", return_value=False), \
             mock.patch.object(phase2_scorer, "_call_llm", side_effect=net_err):
            with self.assertRaises(phase2_scorer.TransientAbort):
                self._run_score_jobs()
        self.assertEqual(
            self._statuses(), {"j1": "un-scored", "j2": "un-scored", "j3": "un-scored"}
        )

    def test_content_failure_still_marks_error(self):
        with mock.patch.object(phase2_scorer, "check_kb_ready", return_value=False), \
             mock.patch.object(phase2_scorer, "_call_llm", side_effect=ValueError("bad json")):
            scored = self._run_score_jobs()  # must NOT raise
        self.assertEqual(scored, [])
        self.assertEqual(self._statuses(), {"j1": "error", "j2": "error", "j3": "error"})


class SchedulerExecuteRunTest(unittest.TestCase):
    """End-to-end stage execution against tiny stand-in scripts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _make_db(self.tmp.name)
        self.log_file = Path(self.tmp.name) / "logs" / "pipeline.log"
        self.sched = scheduler.Scheduler()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _script(self, name: str, exit_code: int) -> str:
        path = Path(self.tmp.name) / name
        path.write_text(textwrap.dedent(f"""\
            import sys
            print("=== {name} ===")
            sys.exit({exit_code})
        """))
        return str(path)

    def _execute(self, stages):
        if get_open_pipeline_run(self.conn) is None:
            start_pipeline_run(self.conn)
        run = get_open_pipeline_run(self.conn)
        with mock.patch.object(scheduler, "STAGES", tuple(stages)), \
             mock.patch.object(scheduler, "LOG_FILE", self.log_file):
            self.sched._execute_run(self.conn, run)
        return run["id"]

    def test_all_stages_succeed(self):
        s1 = self._script("ok1.py", 0)
        s2 = self._script("ok2.py", 0)
        run_id = self._execute([s1, s2])
        self.assertIsNone(get_open_pipeline_run(self.conn))
        row = self.conn.execute(
            "SELECT status, stages_done FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["stages_done"], f"{s1},{s2}")

    def test_tempfail_keeps_run_open_then_resumes(self):
        ok = self._script("ok.py", 0)
        tempfail = self._script("tempfail.py", 75)
        run_id = self._execute([ok, tempfail])

        # Run stays open: first stage done, second pending; backoff armed.
        run = get_open_pipeline_run(self.conn)
        self.assertEqual(run["id"], run_id)
        self.assertEqual(run["stages_done"], ok)
        self.assertEqual(self.sched.tempfail_count, 1)
        self.assertIsNotNone(self.sched.next_attempt_at)

        # "Network is back": same stage list, but the script now succeeds.
        fixed = self._script("tempfail.py", 0)
        self._execute([ok, fixed])
        self.assertIsNone(get_open_pipeline_run(self.conn))
        self.assertEqual(self.sched.tempfail_count, 0)
        row = self.conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(row["status"], "success")

    def test_hard_failure_marks_run_failed(self):
        ok = self._script("ok.py", 0)
        bad = self._script("bad.py", 1)
        run_id = self._execute([ok, bad])
        self.assertIsNone(get_open_pipeline_run(self.conn))
        row = self.conn.execute(
            "SELECT status, last_error FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("exited 1", row["last_error"])


if __name__ == "__main__":
    unittest.main()
