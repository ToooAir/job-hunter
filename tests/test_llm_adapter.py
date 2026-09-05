"""chat_completion / chat_parse learn a reasoning model's parameter quirks
from the first 400 and never send the offending parameter again."""
import unittest
from unittest import mock

import httpx
import openai

from utils import llm


def _bad_request(msg: str) -> openai.BadRequestError:
    req = httpx.Request("POST", "https://api.test/v1/chat")
    return openai.BadRequestError(msg, response=httpx.Response(400, request=req), body=None)


class _FakeClient:
    """Rejects temperature/max_tokens like gpt-5.x, records every call."""

    def __init__(self):
        self.calls = []
        self.chat = mock.Mock()
        self.chat.completions.create.side_effect = self._create
        self.beta = mock.Mock()
        self.beta.chat.completions.parse.side_effect = self._create

    def _create(self, **kw):
        self.calls.append(kw)
        if "temperature" in kw:
            raise _bad_request("Unsupported value: 'temperature' does not support 0.3 with this model.")
        if "max_tokens" in kw:
            raise _bad_request("Unsupported parameter: 'max_tokens' is not supported with this model. "
                               "Use 'max_completion_tokens' instead.")
        return "ok"


class ChatAdapterTest(unittest.TestCase):
    def setUp(self):
        llm._QUIRKS.clear()
        # phase2_scorer's load_dotenv() may have pulled CHAT_REASONING_EFFORT
        # from the developer's .env into this process — tests pin it.
        self._effort = mock.patch.object(llm, "CHAT_REASONING_EFFORT", "")
        self._effort.start()

    def tearDown(self):
        self._effort.stop()
        llm._QUIRKS.clear()

    def test_learns_temperature_then_max_tokens(self):
        c = _FakeClient()
        # first call: 400 on temperature, retry, 400 on max_tokens, retry → ok
        self.assertEqual(llm.chat_completion(c, model="m", messages=[], temperature=0.3, max_tokens=10), "ok")
        self.assertEqual(len(c.calls), 3)
        self.assertEqual(llm._QUIRKS, {"no_temperature", "max_completion_tokens"})
        # second call: both quirks applied up front, single request
        self.assertEqual(llm.chat_completion(c, model="m", messages=[], temperature=0.3, max_tokens=10), "ok")
        self.assertEqual(len(c.calls), 4)
        self.assertEqual(c.calls[-1], {"model": "m", "messages": [], "max_completion_tokens": 10})

    def test_quirk_learned_by_another_thread_still_retries(self):
        # race: a sibling worker recorded the quirk between our adapt and our
        # 400 — the 400 still names a parameter we sent, so retry, don't raise
        c = _FakeClient()
        calls = []

        def create(**kw):
            calls.append(kw)
            if len(calls) == 1:
                llm._QUIRKS.add("no_temperature")   # sibling learned it while our request was in flight
                raise _bad_request("Unsupported value: 'temperature' does not support 0.3")
            return "ok"
        c.chat.completions.create.side_effect = create
        self.assertEqual(llm.chat_completion(c, model="m", messages=[], temperature=0.3), "ok")
        self.assertIn("temperature", calls[0])
        self.assertNotIn("temperature", calls[1])

    def test_unrelated_400_is_raised(self):
        c = _FakeClient()
        c.chat.completions.create.side_effect = _bad_request("context length exceeded")
        with self.assertRaises(openai.BadRequestError):
            llm.chat_completion(c, model="m", messages=[])
        self.assertEqual(llm._QUIRKS, set())

    def test_parse_path_shares_quirks(self):
        c = _FakeClient()
        llm._QUIRKS.add("no_temperature")
        self.assertEqual(llm.chat_parse(c, model="m", messages=[], temperature=0.3), "ok")
        self.assertNotIn("temperature", c.calls[-1])

    def test_reasoning_effort_injected_and_dropped_when_rejected(self):
        c = _FakeClient()
        with mock.patch.object(llm, "CHAT_REASONING_EFFORT", "low"):
            llm.chat_completion(c, model="m", messages=[])
            self.assertEqual(c.calls[-1].get("reasoning_effort"), "low")
            # a model that rejects the parameter: learned, dropped on retry
            c.chat.completions.create.side_effect = [
                _bad_request("Unsupported parameter: 'reasoning_effort'"), "ok"]
            self.assertEqual(llm.chat_completion(c, model="m", messages=[]), "ok")
            self.assertIn("no_reasoning_effort", llm._QUIRKS)

    def test_plain_model_untouched(self):
        c = _FakeClient()
        c.chat.completions.create.side_effect = lambda **kw: c.calls.append(kw) or "ok"
        llm.chat_completion(c, model="m", messages=[], temperature=0.2, max_tokens=5)
        self.assertEqual(c.calls[-1], {"model": "m", "messages": [], "temperature": 0.2, "max_tokens": 5})


if __name__ == "__main__":
    unittest.main()


class KbThresholdTest(unittest.TestCase):
    """The retrieval floor must follow the embedding model (2026-09-04: the
    0.60 mistral floor emptied every retrieval on text-embedding-3-small)."""

    def test_default_per_model(self):
        import os
        import phase2_scorer as ps
        env = {k: v for k, v in os.environ.items() if k != "KB_SCORE_THRESHOLD"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch.object(ps, "emb_model", return_value="text-embedding-3-small"):
                self.assertAlmostEqual(ps._kb_score_threshold(), 0.35)
            with mock.patch.object(ps, "emb_model", return_value="mistral-embed"):
                self.assertAlmostEqual(ps._kb_score_threshold(), 0.60)

    def test_env_override(self):
        import phase2_scorer as ps
        with mock.patch.dict("os.environ", {"KB_SCORE_THRESHOLD": "0.42"}):
            self.assertAlmostEqual(ps._kb_score_threshold(), 0.42)


class ModelResolutionTest(unittest.TestCase):
    """Model names are provider-scoped; another provider's leftovers are ignored."""

    def _env(self, provider, **env):
        return (mock.patch.object(llm, "LLM_PROVIDER", provider),
                mock.patch.dict("os.environ", env, clear=False))

    def _resolve(self, provider, env, clear=("CHAT_MODEL", "TRANSLATION_MODEL", "EMB_MODEL",
                                              "AZURE_CHAT_DEPLOYMENT", "AZURE_TRANSLATION_DEPLOYMENT",
                                              "AZURE_EMB_DEPLOYMENT", "MISTRAL_CHAT_MODEL",
                                              "MISTRAL_TRANSLATION_MODEL", "MISTRAL_EMB_MODEL")):
        import os
        base = {k: v for k, v in os.environ.items() if k not in clear}
        base.update(env)
        with mock.patch.object(llm, "LLM_PROVIDER", provider), \
             mock.patch.dict("os.environ", base, clear=True):
            return llm.chat_model(), llm.translation_model(), llm.emb_model()

    def test_azure_ignores_generic_chat_and_emb_but_honours_generic_translation(self):
        chat, tr, emb = self._resolve("azure", {
            "AZURE_CHAT_DEPLOYMENT": "gpt-5.6-luna", "AZURE_EMB_DEPLOYMENT": "text-embedding-3-small",
            "CHAT_MODEL": "mistral-medium-latest", "EMB_MODEL": "mistral-embed",
            "TRANSLATION_MODEL": "gpt-5-nano"})
        self.assertEqual((chat, tr, emb), ("gpt-5.6-luna", "gpt-5-nano", "text-embedding-3-small"))

    def test_azure_scoped_translation_wins_over_generic(self):
        _, tr, _ = self._resolve("azure", {"AZURE_CHAT_DEPLOYMENT": "luna",
                                           "AZURE_TRANSLATION_DEPLOYMENT": "gpt-5-nano",
                                           "TRANSLATION_MODEL": "mistral-small-2603"})
        self.assertEqual(tr, "gpt-5-nano")

    def test_translation_falls_back_to_chat(self):
        chat, tr, _ = self._resolve("azure", {"AZURE_CHAT_DEPLOYMENT": "luna"})
        self.assertEqual((chat, tr), ("luna", "luna"))

    def test_mistral_scoped_then_generic_then_default(self):
        self.assertEqual(self._resolve("mistral", {"MISTRAL_CHAT_MODEL": "mistral-medium-latest",
                                                   "CHAT_MODEL": "gpt-4o"})[0], "mistral-medium-latest")
        self.assertEqual(self._resolve("mistral", {"CHAT_MODEL": "mistral-large-latest"})[0], "mistral-large-latest")
        # the .env.example sample values are not mistral models → provider default
        self.assertEqual(self._resolve("mistral", {"CHAT_MODEL": "gpt-4o",
                                                   "EMB_MODEL": "text-embedding-3-small"})[::2],
                         ("mistral-small-2603", "mistral-embed"))

    def test_openai_uses_generic(self):
        self.assertEqual(self._resolve("openai", {"CHAT_MODEL": "gpt-5-mini", "EMB_MODEL": "text-embedding-3-large"})[::2],
                         ("gpt-5-mini", "text-embedding-3-large"))

    def test_summary_names_sources(self):
        import os
        with mock.patch.object(llm, "LLM_PROVIDER", "azure"), \
             mock.patch.dict("os.environ", {"AZURE_CHAT_DEPLOYMENT": "luna", "TRANSLATION_MODEL": "nano",
                                            # the deployed .env sets this and it wins over the
                                            # generic var — pin it out (see JOB_HUNTER_SKIP_DOTENV)
                                            "AZURE_TRANSLATION_DEPLOYMENT": ""}):
            summary = llm.model_summary()
        self.assertIn("chat=luna (AZURE_CHAT_DEPLOYMENT)", summary)
        self.assertIn("translation=nano (TRANSLATION_MODEL)", summary)


class KbModelGuardTest(unittest.TestCase):
    def test_mismatch_raises_and_missing_marker_only_warns(self):
        import tempfile, pathlib
        import phase2_scorer as ps
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ps, "emb_model", return_value="text-embedding-3-small"):
                ps.check_kb_model(d)  # no marker → warning only
                pathlib.Path(d, ".kb_model").write_text("mistral-embed")
                with self.assertRaises(ps.KBModelMismatch):
                    ps.check_kb_model(d)
                pathlib.Path(d, ".kb_model").write_text("text-embedding-3-small\n")
                ps.check_kb_model(d)  # same model → fine
