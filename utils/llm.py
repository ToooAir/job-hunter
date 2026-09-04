"""
utils/llm.py — Shared OpenAI-compatible client factory + rate limiter.
Reads LLM_PROVIDER and related env vars; call make_client() / chat_model() / emb_model() anywhere.
Call rate_limit() before every API request to respect provider limits.
Route chat calls through chat_completion() / chat_parse() instead of the raw
client: they adapt request parameters to the model's quirks (reasoning models
reject temperature and max_tokens) and inject CHAT_REASONING_EFFORT.

Supported providers:
  openai   — OpenAI API (default)
  azure    — Azure OpenAI
  mistral  — Mistral AI (JSON mode only, no Structured Outputs). Free-tier quotas
             are per model family (mistral-small / mistral-medium each 20k TPM,
             10 RPM as of 2026-09), so TRANSLATION_MODEL can point the cheap
             translation call at a different family than CHAT_MODEL to use two
             buckets in parallel.
  custom   — Any OpenAI-compatible endpoint (LiteLLM, Ollama, vLLM, …)
"""

import os
import time
import threading

LLM_PROVIDER          = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
AZURE_ENDPOINT        = os.getenv("AZURE_ENDPOINT", "")
AZURE_API_VERSION     = os.getenv("AZURE_API_VERSION", "2024-08-01-preview")
AZURE_CHAT_DEPLOYMENT = os.getenv("AZURE_CHAT_DEPLOYMENT", "gpt-4o")
AZURE_EMB_DEPLOYMENT  = os.getenv("AZURE_EMB_DEPLOYMENT", "text-embedding-3-small")
CUSTOM_BASE_URL       = os.getenv("CUSTOM_BASE_URL", "")
CHAT_MODEL            = os.getenv("CHAT_MODEL", "gpt-4o")
TRANSLATION_MODEL     = os.getenv("TRANSLATION_MODEL", "")   # empty → same as chat_model()
CHAT_REASONING_EFFORT = os.getenv("CHAT_REASONING_EFFORT", "")  # e.g. "low"; empty → not sent
EMB_MODEL             = os.getenv("EMB_MODEL", "text-embedding-3-small")
MISTRAL_API_KEY       = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_BASE_URL      = "https://api.mistral.ai/v1"

# Providers that do NOT support OpenAI Structured Outputs (.parse)
# → phase2_scorer._call_llm will use JSON mode for these
NO_STRUCTURED_OUTPUT_PROVIDERS = {"custom", "mistral"}

# ── Rate limiter ───────────────────────────────────────────────────────────────

# Mistral hard limits (as of 2026-04):
#   RPS  : 1 req/sec  (binding constraint)
#   TPM  : ~500,000   (not binding at 1 RPS × ~4k tokens)
#   RPD  : ~86,400    (theoretical max at 1 RPS)
_PROVIDER_RPS: dict[str, float] = {
    "mistral": 1.0,
}


class _RateLimiter:
    """Thread-safe rate limiter. No-op when rps=0 (all non-Mistral providers)."""

    def __init__(self, rps: float = 0.0):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._last: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._interval == 0.0:
            return
        with self._lock:
            now = time.monotonic()
            gap = self._interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_limiter = _RateLimiter(rps=_PROVIDER_RPS.get(LLM_PROVIDER, 0.0))


def rate_limit() -> None:
    """Call before every LLM API request. Enforces per-provider RPS limits."""
    _limiter.wait()


# ── Client factory ─────────────────────────────────────────────────────────────

def make_client() -> "openai.OpenAI":
    # imported here so stdlib-only consumers (rate_limit, model names) work
    # in environments without the LLM stack (host venv, unit tests)
    import openai

    if LLM_PROVIDER == "azure":
        return openai.AzureOpenAI(
            api_key=OPENAI_API_KEY,
            azure_endpoint=AZURE_ENDPOINT,
            api_version=AZURE_API_VERSION,
        )
    if LLM_PROVIDER == "mistral":
        return openai.OpenAI(
            api_key=MISTRAL_API_KEY,
            base_url=MISTRAL_BASE_URL,
            max_retries=0,  # disable SDK retries; our code handles 60s retry on 429
        )
    if LLM_PROVIDER == "custom":
        return openai.OpenAI(
            api_key=OPENAI_API_KEY or "dummy",
            base_url=CUSTOM_BASE_URL,
        )
    return openai.OpenAI(api_key=OPENAI_API_KEY)


def chat_model() -> str:
    if LLM_PROVIDER == "azure":
        return AZURE_CHAT_DEPLOYMENT
    if LLM_PROVIDER == "mistral":
        return CHAT_MODEL if CHAT_MODEL != "gpt-4o" else "mistral-small-2603"
    return CHAT_MODEL


def translation_model() -> str:
    """Model for the JD German→English translation call.

    Translation is a low-stakes task; on Mistral's per-family rate buckets
    routing it to a different family than the scorer (e.g. mistral-small
    while CHAT_MODEL is mistral-medium) roughly halves the scorer's token
    pressure. Falls back to chat_model() when TRANSLATION_MODEL is unset.
    """
    return TRANSLATION_MODEL or chat_model()


# ── Chat call adapter ──────────────────────────────────────────────────────────
# Reasoning models (GPT-5 family, incl. Azure deployments of them) reject
# temperature != 1, want max_completion_tokens instead of max_tokens, and take
# reasoning_effort. Deployment names are arbitrary, so instead of guessing from
# the model id the adapter learns from the first 400 and remembers the quirk
# for the rest of the process. Learned once per process, not per model: every
# call site here uses the same provider, so the quirks are the same.
_QUIRKS: set[str] = set()
_QUIRKS_LOCK = threading.Lock()
_QUIRK_MARKERS = {
    "temperature": "no_temperature",
    "max_tokens": "max_completion_tokens",
    "reasoning_effort": "no_reasoning_effort",
}


def _adapt_chat_kwargs(kwargs: dict) -> dict:
    kw = dict(kwargs)
    if CHAT_REASONING_EFFORT and "reasoning_effort" not in kw:
        kw["reasoning_effort"] = CHAT_REASONING_EFFORT
    if "no_temperature" in _QUIRKS:
        kw.pop("temperature", None)
    if "max_completion_tokens" in _QUIRKS and "max_tokens" in kw:
        kw["max_completion_tokens"] = kw.pop("max_tokens")
    if "no_reasoning_effort" in _QUIRKS:
        kw.pop("reasoning_effort", None)
    return kw


def _learn_quirk(exc: Exception, sent: dict) -> bool:
    """Record which parameter a 400 complained about.

    True when the 400 names a parameter we actually sent, i.e. a retry with
    adapted kwargs will differ. That includes a quirk another thread learned
    a moment ago: with three workers starting together, two of them get the
    same first 400, and the second must retry too (2026-09-04, a job was
    filed as error because the loser of that race raised instead).
    """
    msg = str(exc)
    adaptable = False
    with _QUIRKS_LOCK:
        for param, quirk in _QUIRK_MARKERS.items():
            if param in msg and param in sent:
                _QUIRKS.add(quirk)
                adaptable = True
    return adaptable


def _call_with_quirks(fn, kwargs: dict):
    """Call fn(**kwargs), learning one rejected parameter per 400 and retrying
    until the request goes through or a 400 teaches nothing new."""
    import openai
    for _ in range(len(_QUIRK_MARKERS) + 1):
        sent = _adapt_chat_kwargs(kwargs)
        try:
            return fn(**sent)
        except openai.BadRequestError as exc:
            if not _learn_quirk(exc, sent):
                raise
    return fn(**_adapt_chat_kwargs(kwargs))


def chat_completion(client, **kwargs):
    """client.chat.completions.create(**kwargs) with model-quirk adaptation."""
    return _call_with_quirks(client.chat.completions.create, kwargs)


def chat_parse(client, **kwargs):
    """client.beta.chat.completions.parse(**kwargs) with model-quirk adaptation."""
    return _call_with_quirks(client.beta.chat.completions.parse, kwargs)


def emb_model() -> str:
    if LLM_PROVIDER == "azure":
        return AZURE_EMB_DEPLOYMENT
    if LLM_PROVIDER == "mistral":
        return EMB_MODEL if EMB_MODEL != "text-embedding-3-small" else "mistral-embed"
    return EMB_MODEL
