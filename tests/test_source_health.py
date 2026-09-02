"""Tests for utils.source_health (dead-source warning)."""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.source_health import record_source_results, warn_silent_sources  # noqa: E402


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
              "updated_at TEXT NOT NULL)")
    return c


class SourceHealthTest(unittest.TestCase):
    def test_silent_runs_accumulate_and_warn_at_threshold(self):
        c = _conn()
        for i in range(1, 3):
            self.assertEqual(record_source_results(c, {"wad": (0, 0)}, warn_after=3), [], i)
        self.assertEqual(record_source_results(c, {"wad": (0, 0)}, warn_after=3), [("wad", 3)])
        self.assertEqual(record_source_results(c, {"wad": (0, 0)}, warn_after=3), [("wad", 4)])

    def test_any_activity_resets_the_counter(self):
        c = _conn()
        record_source_results(c, {"wad": (0, 0)})
        record_source_results(c, {"wad": (0, 0)})
        # skipped-only is still a live endpoint (it saw known postings)
        record_source_results(c, {"wad": (0, 12)})
        self.assertEqual(record_source_results(c, {"wad": (0, 0)}), [])
        self.assertEqual(c.execute("SELECT value FROM app_state WHERE key = "
                                   "'source_silent_runs:wad'").fetchone()[0], "1")

    def test_sources_are_independent(self):
        c = _conn()
        for _ in range(3):
            silent = record_source_results(c, {"wad": (0, 0), "ba": (5, 100)})
        self.assertEqual(silent, [("wad", 3)])

    def test_warn_logs_once_per_silent_source(self):
        c = _conn()
        for _ in range(2):
            record_source_results(c, {"wad": (0, 0)})
        with self.assertLogs("utils.source_health", level="WARNING") as cm:
            silent = warn_silent_sources(c, {"wad": (0, 0), "ba": (1, 0)})
        self.assertEqual(silent, [("wad", 3)])
        self.assertEqual(len(cm.output), 1)
        self.assertIn("wad", cm.output[0])


if __name__ == "__main__":
    unittest.main()
