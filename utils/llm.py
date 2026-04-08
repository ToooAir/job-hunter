"""
utils/llm.py — Shared OpenAI-compatible client factory + rate limiter.
Reads LLM_PROVIDER and related env vars; call make_client() / chat_model() / emb_model() anywhere.
Call rate_limit() before every API request to respect provider limits.

Supported providers:
  openai   — OpenAI API (default)
  azure    — Azure OpenAI
  mistral  — Mistral AI (1 RPS hard limit; JSON mode only, no Structured Outputs)
  custom   — Any OpenAI-compatible endpoint (LiteLLM, Ollama, vLLM, …)
"""

import os
import time
import threading
import openai

LLM_PROVIDER          = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
AZURE_ENDPOINT        = os.getenv("AZURE_ENDPOINT", "")
AZURE_API_VERSION     = os.getenv("AZURE_API_VERSION", "2024-08-01-preview")
AZURE_CHAT_DEPLOYMENT = os.getenv("AZURE_CHAT_DEPLOYMENT", "gpt-4o")
AZURE_EMB_DEPLOYMENT  = os.getenv("AZURE_EMB_DEPLOYMENT", "text-embedding-3-small")
CUSTOM_BASE_URL       = os.getenv("CUSTOM_BASE_URL", "")
CHAT_MODEL            = os.getenv("CHAT_MODEL", "gpt-4o")
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

def make_client() -> openai.OpenAI:
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


def emb_model() -> str:
    if LLM_PROVIDER == "azure":
        return AZURE_EMB_DEPLOYMENT
    if LLM_PROVIDER == "mistral":
        return EMB_MODEL if EMB_MODEL != "text-embedding-3-small" else "mistral-embed"
    return EMB_MODEL
