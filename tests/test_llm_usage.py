"""LLM usage ledger + daily budget gate (utils/llm.py).

The ledger is what makes the budget gate possible, and the budget gate is the
only thing standing between a runaway scoring loop and an invoice: a free-tier
quota fails safe (the pipeline stops), a pay-as-you-go card does not.

Every test pins the relevant env vars: .env leaks into the test process through
the module-level load_dotenv() calls (see tests/test_llm_adapter.py).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import llm  # noqa: E402


class _Usage:
    def __init__(self, prompt, completion, cached=0):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = mock.Mock(cached_tokens=cached)


class _Resp:
    def __init__(self, usage=None):
        self.usage = usage


class _FakeChatClient:
    """Minimal stand-in: returns a response carrying a usage object."""

    def __init__(self, usage):
        self.chat = mock.Mock()
        self.chat.completions.create.return_value = _Resp(usage)
        self.beta = mock.Mock()
        self.beta.chat.completions.parse.return_value = _Resp(usage)
        self.embeddings = mock.Mock()
        self.embeddings.create.return_value = _Resp(usage)


class _LedgerCase(unittest.TestCase):
    """Fresh ledger file + reset spend counters + pinned env for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "llm_usage.jsonl"
        self._env = mock.patch.dict(os.environ, {
            "LLM_USAGE_PATH":       str(self.ledger),
            "LLM_DAILY_BUDGET_USD": "",
            "LLM_PRICE_CHAT":       "",
            "LLM_PRICE_TRANSLATION": "",
            "LLM_PRICE_EMB":        "",
        })
        self._env.start()
        self._reset_spend()
        llm.set_budget_override(None)
        llm._unpriced.clear()
        llm.set_job_context(None)

    def tearDown(self):
        self._env.stop()
        self._reset_spend()
        llm.set_budget_override(None)
        self._tmp.cleanup()

    def _reset_spend(self):
        llm._spend_date = ""
        llm._spend_baseline = 0.0
        llm._spend_process = 0.0

    def lines(self):
        if not self.ledger.exists():
            return []
        return [json.loads(ln) for ln in self.ledger.read_text().splitlines() if ln.strip()]

    def write_ledger(self, *records):
        with self.ledger.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")


class RecordUsageTest(_LedgerCase):
    def test_chat_call_appends_a_priced_line(self):
        client = _FakeChatClient(_Usage(prompt=10_000, completion=1_000, cached=4_000))
        llm.set_job_context("job-1")
        llm.chat_completion(client, model="gpt-5.6-luna", messages=[])

        (rec,) = self.lines()
        self.assertEqual(rec["model"], "gpt-5.6-luna")
        self.assertEqual(rec["kind"], "chat")
        self.assertEqual(rec["prompt_tokens"], 10_000)
        self.assertEqual(rec["cached_tokens"], 4_000)
        self.assertEqual(rec["completion_tokens"], 1_000)
        self.assertEqual(rec["job_id"], "job-1")
        # 6k uncached @0.20 + 4k cached @0.10 + 1k out @1.20, per 1M
        expected = (6_000 * 0.20 + 4_000 * 0.10 + 1_000 * 1.20) / 1e6
        self.assertAlmostEqual(rec["est_usd"], expected, places=10)
        self.assertAlmostEqual(llm.spend_today(), expected, places=10)

    def test_cached_tokens_are_billed_at_the_cached_rate(self):
        """Without the discount the estimate runs ~30-45% high on Luna."""
        cold = _FakeChatClient(_Usage(prompt=10_000, completion=0, cached=0))
        llm.chat_completion(cold, model="gpt-5.6-luna", messages=[])
        warm = _FakeChatClient(_Usage(prompt=10_000, completion=0, cached=10_000))
        llm.chat_completion(warm, model="gpt-5.6-luna", messages=[])

        cold_rec, warm_rec = self.lines()
        self.assertAlmostEqual(warm_rec["est_usd"], cold_rec["est_usd"] / 2, places=10)

    def test_chat_parse_is_recorded_too(self):
        client = _FakeChatClient(_Usage(prompt=100, completion=10))
        llm.chat_parse(client, model="gpt-5.6-luna", messages=[])
        self.assertEqual(len(self.lines()), 1)

    def test_unknown_model_logs_tokens_but_no_cost(self):
        client = _FakeChatClient(_Usage(prompt=1_000, completion=100))
        llm.chat_completion(client, model="some-new-deployment", messages=[])

        (rec,) = self.lines()
        self.assertIsNone(rec["est_usd"])
        self.assertEqual(rec["prompt_tokens"], 1_000)
        self.assertEqual(llm.spend_today(), 0.0)   # invisible to the gate — hence the warning

    def test_response_without_usage_is_skipped_silently(self):
        client = _FakeChatClient(None)
        llm.chat_completion(client, model="gpt-5.6-luna", messages=[])
        self.assertEqual(self.lines(), [])

    def test_accounting_failure_never_breaks_the_call(self):
        with mock.patch.dict(os.environ, {"LLM_USAGE_PATH": "/nonexistent-dir/x.jsonl"}):
            client = _FakeChatClient(_Usage(prompt=100, completion=10))
            self.assertIsNotNone(llm.chat_completion(client, model="gpt-5.6-luna", messages=[]))

    def test_embed_records_kind_emb_and_rate_limits(self):
        client = _FakeChatClient(_Usage(prompt=500, completion=0))
        with mock.patch.object(llm, "rate_limit") as rl:
            llm.embed(client, ["a", "b"], model="text-embedding-3-small")
        rl.assert_called_once()
        client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small", input=["a", "b"])
        (rec,) = self.lines()
        self.assertEqual(rec["kind"], "emb")
        self.assertAlmostEqual(rec["est_usd"], 500 * 0.02 / 1e6, places=12)


class PriceOverrideTest(_LedgerCase):
    def test_env_override_beats_the_table(self):
        with mock.patch.dict(os.environ, {"LLM_PRICE_CHAT": "1/0.5/2"}):
            client = _FakeChatClient(_Usage(prompt=1_000, completion=1_000, cached=0))
            llm.chat_completion(client, model="gpt-5.6-luna", messages=[])
        (rec,) = self.lines()
        self.assertAlmostEqual(rec["est_usd"], (1_000 * 1 + 1_000 * 2) / 1e6, places=12)

    def test_single_number_override_prices_an_embedding_model(self):
        with mock.patch.dict(os.environ, {"LLM_PRICE_EMB": "0.05"}):
            client = _FakeChatClient(_Usage(prompt=1_000, completion=0))
            llm.embed(client, ["x"], model="whatever-embed")
        (rec,) = self.lines()
        self.assertAlmostEqual(rec["est_usd"], 1_000 * 0.05 / 1e6, places=12)

    def test_garbage_override_falls_back_to_the_table(self):
        with mock.patch.dict(os.environ, {"LLM_PRICE_CHAT": "cheap"}):
            self.assertEqual(llm._price_for("gpt-5.6-luna", "chat"), (0.20, 0.10, 1.20))

    def test_longest_model_key_wins(self):
        self.assertEqual(llm._price_for("gpt-4o-mini", "chat"), (0.15, 0.075, 0.60))
        self.assertEqual(llm._price_for("gpt-4o", "chat"), (2.50, 1.25, 10.00))


class BudgetGateTest(_LedgerCase):
    def test_no_budget_configured_never_raises(self):
        self.assertIsNone(llm.daily_budget())
        llm.check_budget()

    def test_under_budget_passes(self):
        with mock.patch.dict(os.environ, {"LLM_DAILY_BUDGET_USD": "2.0"}):
            client = _FakeChatClient(_Usage(prompt=10_000, completion=1_000))
            llm.chat_completion(client, model="gpt-5.6-luna", messages=[])
            llm.check_budget()

    def test_reaching_the_budget_raises(self):
        with mock.patch.dict(os.environ, {"LLM_DAILY_BUDGET_USD": "0.001"}):
            client = _FakeChatClient(_Usage(prompt=1_000_000, completion=0))  # $0.20
            llm.chat_completion(client, model="gpt-5.6-luna", messages=[])
            with self.assertRaises(llm.BudgetExceeded) as ctx:
                llm.check_budget()
        self.assertIn("daily LLM budget reached", str(ctx.exception))

    def test_spend_by_another_process_counts(self):
        """pipeline, dashboard and apply_api share one ledger file."""
        today = llm._today()
        self.write_ledger(
            {"ts": f"{today}T08:00:00", "model": "gpt-5.6-luna", "est_usd": 1.5},
            {"ts": "2020-01-01T08:00:00", "model": "gpt-5.6-luna", "est_usd": 99.0},
            {"ts": f"{today}T08:01:00", "model": "x", "est_usd": None},
        )
        with mock.patch.dict(os.environ, {"LLM_DAILY_BUDGET_USD": "2.0"}):
            self.assertAlmostEqual(llm.spend_today(), 1.5, places=10)
            llm.check_budget()
            client = _FakeChatClient(_Usage(prompt=3_000_000, completion=0))  # +$0.60
            llm.chat_completion(client, model="gpt-5.6-luna", messages=[])
            with self.assertRaises(llm.BudgetExceeded):
                llm.check_budget()

    def test_a_corrupt_ledger_line_does_not_stop_the_tally(self):
        today = llm._today()
        self.ledger.write_text(
            "not json\n"
            + json.dumps({"ts": f"{today}T08:00:00", "est_usd": 0.25}) + "\n",
            encoding="utf-8")
        self.assertAlmostEqual(llm.spend_today(), 0.25, places=10)

    def test_override_beats_env_and_clears(self):
        with mock.patch.dict(os.environ, {"LLM_DAILY_BUDGET_USD": "0.0001"}):
            llm.set_budget_override(10.0)
            self.assertEqual(llm.daily_budget(), 10.0)
            llm.set_budget_override(None)
            self.assertEqual(llm.daily_budget(), 0.0001)

    def test_unparseable_or_negative_budget_is_treated_as_unlimited(self):
        for bad in ("abc", "-1"):
            with mock.patch.dict(os.environ, {"LLM_DAILY_BUDGET_USD": bad}):
                self.assertIsNone(llm.daily_budget())

    def test_zero_budget_blocks_everything(self):
        with mock.patch.dict(os.environ, {"LLM_DAILY_BUDGET_USD": "0"}):
            self.assertEqual(llm.daily_budget(), 0.0)
            with self.assertRaises(llm.BudgetExceeded):
                llm.check_budget()


if __name__ == "__main__":
    unittest.main()


class ScorerBudgetAbortTest(unittest.TestCase):
    """The budget gate must leave work undone, never file it as a failure.

    2026-09-04: the exhausted-quota path marked 117 jobs 'error', which drops
    them for good. Budget exhaustion takes the transient road instead — exit 75,
    scheduler backs off, the jobs are still un-scored tomorrow.

    Needs the LLM SDK stack (phase2_scorer imports openai) — run in the container:
        docker exec job-hunter-pipeline-1 python3 -m unittest tests.test_llm_usage -v
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "jobs.db")
        self._env = mock.patch.dict(os.environ, {
            "LLM_USAGE_PATH": str(Path(self._tmp.name) / "usage.jsonl"),
            "LLM_DAILY_BUDGET_USD": "0",     # nothing may be spent
        })
        self._env.start()
        llm._spend_date, llm._spend_baseline, llm._spend_process = "", 0.0, 0.0
        llm.set_budget_override(None)

    def tearDown(self):
        self._env.stop()
        llm.set_budget_override(None)
        self._tmp.cleanup()

    def test_exhausted_budget_aborts_without_marking_any_job_error(self):
        import phase2_scorer
        from utils.db import init_db

        conn = init_db(self.db)
        conn.execute(
            "INSERT INTO jobs (id, company, title, url, source, raw_jd_text, "
            "  fetched_at, location, status) VALUES (?,?,?,?,?,?,?,?,?)",
            ("budget-1", "Acme", "Backend Engineer", "https://example.com/budget-1",
             "lever", "We are looking for a backend engineer. " * 20,
             "2026-09-05T08:00:00", "Hamburg, Germany", "un-scored"),
        )
        conn.commit()
        conn.close()

        with mock.patch.object(phase2_scorer, "make_client", return_value=mock.Mock()):
            with self.assertRaises(phase2_scorer.TransientAbort) as ctx:
                phase2_scorer.score_jobs(db_path=self.db,
                                         qdrant_path=str(Path(self._tmp.name) / "no-kb"))
        self.assertIn("budget", str(ctx.exception))

        conn = init_db(self.db)
        row = conn.execute("SELECT status FROM jobs WHERE id = 'budget-1'").fetchone()
        conn.close()
        self.assertEqual(row["status"], "un-scored")
