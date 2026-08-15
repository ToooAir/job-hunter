"""Tests for utils/voice.py and its injection point in phase2_scorer.

The load-bearing property is that ONE function (_qdrant_query) adds the voice
block, so the cover-letter generator and the Stage-1 verifier can never be shown
different source material. When they diverge, the verifier flags the candidate's
real motivation as a fabricated claim.

Run:  python -m unittest discover tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import voice  # noqa: E402
from utils.voice import VOICE_HEADER, append_voice, load_voice  # noqa: E402

EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "candidate_kb" / "voice.md.example"


class LoadVoiceTest(unittest.TestCase):
    def setUp(self):
        voice._cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "voice.md"

    def test_missing_file_is_not_an_error(self):
        """voice.md is optional — without it nothing about the KB changes."""
        self.assertEqual(load_voice(self.path), "")

    def test_blank_file_reads_as_absent(self):
        self.path.write_text("   \n\n  ", encoding="utf-8")
        self.assertEqual(load_voice(self.path), "")

    def test_reads_and_strips(self):
        self.path.write_text("\n I keep coming back to data work.\n\n", encoding="utf-8")
        self.assertEqual(load_voice(self.path), "I keep coming back to data work.")

    def test_preamble_above_the_fence_is_not_voice(self):
        """The top of the file explains what the file is for. Feeding that to
        the generator would have it write about writing cover letters."""
        self.path.write_text(
            "# Voice\n\nCopy this file and replace every line with your own words.\n"
            "\n---\n\n"
            "I keep coming back to data work.\n",
            encoding="utf-8",
        )
        out = load_voice(self.path)
        self.assertEqual(out, "I keep coming back to data work.")
        self.assertNotIn("Copy this file", out)

    def test_todo_blocks_are_dropped(self):
        """An unfilled 'TODO — why this country, in your own words' reads to the
        LLM as an instruction to write exactly that, with nothing to ground it."""
        self.path.write_text(
            "---\n\n"
            "## What I want next\n\n"
            "I want to design the systems, not just operate them.\n\n"
            "TODO — why this country, why this city, in your own words.\n\n"
            "## Problems I like\n\nMessy inputs.\n",
            encoding="utf-8",
        )
        out = load_voice(self.path)
        self.assertIn("I want to design the systems", out)
        self.assertIn("Messy inputs.", out)
        self.assertNotIn("TODO", out)
        self.assertNotIn("why this city", out)

    def test_heading_emptied_by_a_todo_is_dropped(self):
        """Left in, a bare heading reads as a prompt to fill the gap."""
        self.path.write_text(
            "---\n\n"
            "## Problems I like\n\nMessy inputs.\n\n"
            "## How I work with people\n\n"
            "TODO — two or three plain sentences.\n",
            encoding="utf-8",
        )
        out = load_voice(self.path)
        self.assertIn("Messy inputs.", out)
        self.assertNotIn("How I work with people", out)

    def test_file_of_only_notes_reads_as_absent(self):
        """A freshly copied template with nothing filled in must inject nothing."""
        self.path.write_text(
            "# Voice\n\nExplanation for the human.\n\n---\n\n"
            "TODO — write why this kind of work.\n\n"
            "TODO — write what you want next.\n",
            encoding="utf-8",
        )
        self.assertEqual(load_voice(self.path), "")

    def test_edit_takes_effect_without_a_restart(self):
        """The profile loader's lru_cache needs an apply_api restart to pick up
        edits; voice.md is keyed on the file stat so iterating on it does not."""
        self.path.write_text("first version, long enough to differ", encoding="utf-8")
        self.assertEqual(load_voice(self.path), "first version, long enough to differ")

        self.path.write_text("second version, a different length", encoding="utf-8")
        self.assertEqual(load_voice(self.path), "second version, a different length")


class AppendVoiceTest(unittest.TestCase):
    def setUp(self):
        voice._cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "voice.md"

    def test_context_untouched_when_no_voice_file(self):
        ctx = "[來源: projects.md]\n- Built a thing."
        self.assertEqual(append_voice(ctx, self.path), ctx)

    def test_voice_is_appended_with_a_named_source(self):
        self.path.write_text("I like making slow systems boring.", encoding="utf-8")
        out = append_voice("[來源: projects.md]\n- Built a thing.", self.path)

        self.assertIn("- Built a thing.", out)          # retrieved chunks survive
        self.assertIn(VOICE_HEADER, out)                # prompt refers to this label
        self.assertIn("I like making slow systems boring.", out)
        self.assertIn("\n---\n", out)                   # same separator as chunks

    def test_appended_even_when_retrieval_found_nothing(self):
        """Motivation is relevant to every letter; which projects are relevant
        depends on the job. So the voice block is unconditional."""
        self.path.write_text("I like making slow systems boring.", encoding="utf-8")
        out = append_voice("[No relevant experience found in KB]", self.path)
        self.assertIn("I like making slow systems boring.", out)


class QdrantQueryInjectionTest(unittest.TestCase):
    """The single choke point: every KB context in the system is built here."""

    def setUp(self):
        voice._cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "voice.md"
        self.path.write_text("I like making slow systems boring.", encoding="utf-8")

        # point the module default at the temp file for the duration of the test
        self._real_default = voice.DEFAULT_VOICE_PATH
        voice.DEFAULT_VOICE_PATH = self.path
        self.addCleanup(lambda: setattr(voice, "DEFAULT_VOICE_PATH", self._real_default))

    @staticmethod
    def _fake_qdrant(hits):
        class _Hit:
            def __init__(self, score, source, text):
                self.score = score
                self.payload = {"source": source, "text": text}

        class _Result:
            points = [_Hit(*h) for h in hits]

        class _Qdrant:
            def query_points(self, **kwargs):
                return _Result()

        return _Qdrant()

    def test_retrieved_context_carries_voice(self):
        from phase2_scorer import _qdrant_query

        out = _qdrant_query(
            self._fake_qdrant([(0.91, "projects.md", "- Built a RAG pipeline.")]),
            [0.0] * 4,
            top_k=5,
        )
        self.assertIn("- Built a RAG pipeline.", out)
        self.assertIn("I like making slow systems boring.", out)

    def test_below_threshold_fallback_still_carries_voice(self):
        from phase2_scorer import _qdrant_query

        out = _qdrant_query(
            self._fake_qdrant([(0.10, "projects.md", "- Unrelated.")]),
            [0.0] * 4,
            top_k=5,
        )
        self.assertIn("[No relevant experience found in KB]", out)
        self.assertIn("I like making slow systems boring.", out)


class VoiceIsNotIndexedTest(unittest.TestCase):
    """voice.md must not reach Qdrant: it is injected unconditionally, so
    indexing it would double-inject it and let a motivation chunk displace a
    relevant project from the handful of retrieval slots."""

    def test_kb_loader_skips_voice_md(self):
        source = (Path(__file__).resolve().parents[1] / "utils" / "kb_loader.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('md_file.name == "voice.md"', source)

    def test_freshness_check_skips_voice_md(self):
        """voice.md is never indexed, so it must not trigger the 'rebuild your
        KB' warning every time the candidate edits it."""
        from phase2_scorer import check_kb_fresh

        with tempfile.TemporaryDirectory() as qdir, tempfile.TemporaryDirectory() as kbdir:
            (Path(qdir) / ".kb_built_at").write_text("2020-01-01T00:00:00", encoding="utf-8")
            (Path(kbdir) / "voice.md").write_text("newer than the KB", encoding="utf-8")

            with self.assertLogs("phase2_scorer", level="WARNING") as caught:
                logging_probe = __import__("logging").getLogger("phase2_scorer")
                logging_probe.warning("probe")  # ensure assertLogs has something
                check_kb_fresh(qdir, kbdir)
            self.assertNotIn("voice.md", "\n".join(caught.output))


class ExampleTemplateTest(unittest.TestCase):
    def test_example_exists_and_is_loadable(self):
        text = load_voice(EXAMPLE_PATH)
        self.assertTrue(text)
        self.assertIn("## Why this kind of work", text)

    def test_example_uses_fictional_data(self):
        """This repo is public and the template ships in it: the example must
        read as an obvious placeholder, never as a real CV. Naming the real
        employers here — even to assert their absence — would put them in git."""
        text = load_voice(EXAMPLE_PATH)
        self.assertIn("Musterfirma", text)


if __name__ == "__main__":
    unittest.main()
