"""
utils/llm.py — Shared OpenAI-compatible client factory + rate limiter.
Reads LLM_PROVIDER and related env vars; call make_client() / chat_model() /
translation_model() / emb_model() anywhere (see "Model names" for how the
provider-scoped and generic env vars resolve).
Call rate_limit() before every API request to respect provider limits.
Route chat calls through chat_completion() / chat_parse() and embedding calls
through embed() instead of the raw client: they adapt request parameters to the
model's quirks (reasoning models reject temperature and max_tokens), inject
CHAT_REASONING_EFFORT, and record token usage + estimated cost (see "Usage
accounting").

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

import json
import logging
import os
import sys
import threading
import time

log = logging.getLogger(__name__)

LLM_PROVIDER          = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
AZURE_ENDPOINT        = os.getenv("AZURE_ENDPOINT", "")
AZURE_API_VERSION     = os.getenv("AZURE_API_VERSION", "2024-08-01-preview")
CUSTOM_BASE_URL       = os.getenv("CUSTOM_BASE_URL", "")
CHAT_REASONING_EFFORT = os.getenv("CHAT_REASONING_EFFORT", "")  # e.g. "low"; empty → not sent
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


# ── Model names ────────────────────────────────────────────────────────────────
# Every provider has its own set of model names, so switching LLM_PROVIDER
# switches the whole set and another provider's leftovers in .env stay
# harmless (2026-09-04: TRANSLATION_MODEL=mistral-small-2603 left over under
# azure was sent as a deployment name → 404 → silent translation failure).
#
# Resolution order per kind (CHAT / TRANSLATION / EMB):
#   1. provider-scoped: AZURE_<kind>_DEPLOYMENT, MISTRAL_<kind>_MODEL,
#      OPENAI_<kind>_MODEL, CUSTOM_<kind>_MODEL
#   2. generic CHAT_MODEL / TRANSLATION_MODEL / EMB_MODEL — for openai/custom
#      (and mistral, historically); azure only honours the generic
#      TRANSLATION_MODEL, its chat/embedding are deployments by definition
#   3. provider default
# TRANSLATION falls back to the chat model when nothing is set.
_KINDS = ("CHAT", "TRANSLATION", "EMB")
_GENERIC_VAR = {"CHAT": "CHAT_MODEL", "TRANSLATION": "TRANSLATION_MODEL", "EMB": "EMB_MODEL"}
_GENERIC_ALLOWED = {"azure": {"TRANSLATION"}}   # default: every kind
_PROVIDER_DEFAULTS = {
    "mistral": {"CHAT": "mistral-small-2603", "EMB": "mistral-embed"},
    "azure":   {"CHAT": "gpt-4o", "EMB": "text-embedding-3-small"},
    "openai":  {"CHAT": "gpt-4o", "EMB": "text-embedding-3-small"},
    "custom":  {"CHAT": "gpt-4o", "EMB": "text-embedding-3-small"},
}
# .env.example ships these as the generic samples; on mistral they are not
# real model names, so they count as "unset" there (pre-existing behaviour).
_SAMPLE_GENERIC = {"gpt-4o", "text-embedding-3-small"}


def _scoped_var(kind: str) -> str:
    if LLM_PROVIDER == "azure":
        return f"AZURE_{kind}_DEPLOYMENT"
    return f"{LLM_PROVIDER.upper()}_{kind}_MODEL"


def _resolve(kind: str) -> tuple[str, str]:
    """Return (model name, where it came from) for a kind in _KINDS."""
    var = _scoped_var(kind)
    val = os.getenv(var, "")
    if val:
        return val, var
    if kind in _GENERIC_ALLOWED.get(LLM_PROVIDER, set(_KINDS)):
        gvar = _GENERIC_VAR[kind]
        gval = os.getenv(gvar, "")
        if gval and not (LLM_PROVIDER == "mistral" and gval in _SAMPLE_GENERIC):
            return gval, gvar
    default = _PROVIDER_DEFAULTS.get(LLM_PROVIDER, _PROVIDER_DEFAULTS["openai"]).get(kind, "")
    return default, "default"


def chat_model() -> str:
    return _resolve("CHAT")[0]


def translation_model() -> str:
    """Model for the JD German→English translation call.

    Translation is a low-stakes task; route it to a cheaper model or another
    rate bucket (mistral-small next to mistral-medium, gpt-5-nano next to
    luna). Falls back to chat_model() when nothing is configured.
    """
    return _resolve("TRANSLATION")[0] or chat_model()


def model_summary() -> str:
    """One line for startup logs: which model each kind resolved to and from where."""
    parts = []
    for kind in _KINDS:
        val, src = _resolve(kind)
        if kind == "TRANSLATION" and not val:
            val, src = chat_model(), "= chat"
        parts.append(f"{kind.lower()}={val} ({src})")
    return " | ".join(parts)


# ── Usage accounting + daily budget gate ──────────────────────────────────────
# Mistral's free tier failed safe: when the quota was gone the pipeline stopped.
# Azure bills a card instead, so a runaway loop or an accidental full-pool
# rescore is an invoice, not an outage. Every chat/embedding call appends one
# JSON line to data/llm_usage.jsonl with token counts and an ESTIMATED cost
# (the provider's invoice is the authority), and phase2_scorer calls
# check_budget() before each job so the day's running total can stop the run
# via TransientAbort → exit 75 → the jobs stay un-scored for tomorrow.
#
# Single-line appends under "a" mode use O_APPEND, so the pipeline, dashboard
# and apply_api containers can share the file without interleaving.

USAGE_PATH_VAR      = "LLM_USAGE_PATH"
_DEFAULT_USAGE_PATH = "./data/llm_usage.jsonl"
_SCRIPT             = os.path.basename(sys.argv[0]) if sys.argv else ""

# $ per 1M tokens: (input, cached input, output). Provider list prices as of
# 2026-09 — an estimate, not the bill. Matched as a substring of the model /
# deployment name (longest key wins), so "gpt-5.6-luna" hits "luna". Override
# per kind with LLM_PRICE_CHAT / LLM_PRICE_TRANSLATION / LLM_PRICE_EMB, either
# "in/cached/out" or a single number (used for input and cached).
_PRICES: dict[str, tuple[float, float, float]] = {
    "luna":                   (0.20, 0.10,  1.20),
    "gpt-5-nano":             (0.05, 0.005, 0.40),
    "gpt-4o-mini":            (0.15, 0.075, 0.60),
    "gpt-4o":                 (2.50, 1.25, 10.00),
    "text-embedding-3-small": (0.02, 0.02,  0.00),
    "text-embedding-3-large": (0.13, 0.13,  0.00),
    "mistral-small":          (0.10, 0.10,  0.30),
    "mistral-medium":         (0.40, 0.40,  2.00),
    "mistral-embed":          (0.01, 0.01,  0.00),
}


class BudgetExceeded(RuntimeError):
    """The day's estimated LLM spend has reached LLM_DAILY_BUDGET_USD.

    Callers must treat this as transient — leave the work undone for the next
    run — never as a per-item failure (2026-09-04: an exhausted-quota path
    filed 117 jobs as permanent errors).
    """


_usage_lock      = threading.Lock()
_budget_override: float | None = None
_spend_date      = ""     # local date the two counters below belong to
_spend_baseline  = 0.0    # other processes' spend that day, read from the ledger
_spend_process   = 0.0    # this process's spend since that read
_unpriced        : set[str] = set()
_ctx             = threading.local()


def _usage_path() -> str:
    """Resolved per call so tests (and scripts) can redirect it via the env var."""
    return os.getenv(USAGE_PATH_VAR, _DEFAULT_USAGE_PATH)


def _today() -> str:
    """Local date. All three containers run TZ=Europe/Berlin, so the budget day
    matches the one the user reasons in (same clock as applied_at)."""
    return time.strftime("%Y-%m-%d")


def set_job_context(job_id: str | None) -> None:
    """Tag this thread's subsequent usage lines with a job id (scorer workers)."""
    _ctx.job_id = job_id


def _kind_for(model: str) -> str:
    if model == emb_model():
        return "emb"
    if model != chat_model() and model == translation_model():
        return "translation"
    return "chat"


def _price_for(model: str, kind: str) -> tuple[float, float, float] | None:
    raw = os.getenv(f"LLM_PRICE_{kind.upper()}", "").strip()
    if raw:
        try:
            parts = [float(p) for p in raw.split("/")]
        except ValueError:
            log.warning("LLM_PRICE_%s=%r is not a price — falling back to the table",
                        kind.upper(), raw)
            parts = []
        if len(parts) == 1:
            return (parts[0], parts[0], 0.0)
        if len(parts) == 3:
            return (parts[0], parts[1], parts[2])
    low = (model or "").lower()
    for key in sorted(_PRICES, key=len, reverse=True):
        if key in low:
            return _PRICES[key]
    return None


def _read_day_total(day: str) -> float:
    """Sum est_usd for one local day across all processes."""
    total = 0.0
    try:
        with open(_usage_path(), encoding="utf-8") as fh:
            for line in fh:
                if day not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if str(rec.get("ts", "")).startswith(day) and rec.get("est_usd"):
                    total += float(rec["est_usd"])
    except FileNotFoundError:
        return 0.0
    except OSError as exc:
        log.warning("LLM usage ledger unreadable (%s) — the budget gate sees $0 spent", exc)
    return total


def _roll_day_locked() -> None:
    """Re-read the ledger when the calendar day changed (long-lived processes)."""
    global _spend_date, _spend_baseline, _spend_process
    day = _today()
    if day == _spend_date:
        return
    _spend_date     = day
    _spend_process  = 0.0
    _spend_baseline = _read_day_total(day)


def spend_today() -> float:
    """Estimated $ spent today: the ledger's total when this process first
    looked, plus everything this process has spent since."""
    with _usage_lock:
        _roll_day_locked()
        return _spend_baseline + _spend_process


def daily_budget() -> float | None:
    """Budget in $ for one local day; None = unlimited (the historical default)."""
    if _budget_override is not None:
        return _budget_override
    raw = os.getenv("LLM_DAILY_BUDGET_USD", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        log.warning("LLM_DAILY_BUDGET_USD=%r is not a number — no budget enforced", raw)
        return None
    if val < 0:
        log.warning("LLM_DAILY_BUDGET_USD=%s is negative — no budget enforced", raw)
        return None
    return val


def set_budget_override(usd: float | None) -> None:
    """Per-run override for the --budget flag; never touches .env (an .env edit
    only takes effect after `docker compose up -d`)."""
    global _budget_override
    _budget_override = usd


def check_budget() -> None:
    """Raise BudgetExceeded when today's estimated spend has reached the budget."""
    budget = daily_budget()
    if budget is None:
        return
    spent = spend_today()
    if spent >= budget:
        raise BudgetExceeded(
            f"daily LLM budget reached — est. ${spent:.4f} spent today "
            f">= ${budget:.2f} (LLM_DAILY_BUDGET_USD)"
        )


def _record_usage(model: str, resp, kind: str | None = None) -> None:
    """Book one call into the day's total and append it to the ledger.

    Never raises: accounting must not take down a call path that worked.
    """
    global _spend_process
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:      # fakes in tests, providers that omit usage
            return
        kind       = kind or _kind_for(model)
        prompt     = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        details    = getattr(usage, "prompt_tokens_details", None)
        cached     = min(int(getattr(details, "cached_tokens", 0) or 0), prompt)
        price      = _price_for(model, kind)
        if price is None:
            est = None
            with _usage_lock:
                unseen = model not in _unpriced
                _unpriced.add(model)
            if unseen:
                log.warning(
                    "no price known for model %r — its spend stays invisible to "
                    "LLM_DAILY_BUDGET_USD; set LLM_PRICE_%s", model, kind.upper())
        else:
            p_in, p_cached, p_out = price
            est = ((prompt - cached) * p_in + cached * p_cached + completion * p_out) / 1e6
            with _usage_lock:
                _roll_day_locked()
                _spend_process += est
        rec = {
            "ts":                time.strftime("%Y-%m-%dT%H:%M:%S"),
            "script":            _SCRIPT,
            "model":             model,
            "kind":              kind,
            "prompt_tokens":     prompt,
            "cached_tokens":     cached,
            "completion_tokens": completion,
            "est_usd":           est,
            "job_id":            getattr(_ctx, "job_id", None),
        }
        with open(_usage_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:   # noqa: BLE001 — accounting is never fatal
        log.warning("LLM usage not recorded: %s", exc)


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
    resp = _call_with_quirks(client.chat.completions.create, kwargs)
    _record_usage(kwargs.get("model", ""), resp)
    return resp


def chat_parse(client, **kwargs):
    """client.beta.chat.completions.parse(**kwargs) with model-quirk adaptation."""
    resp = _call_with_quirks(client.beta.chat.completions.parse, kwargs)
    _record_usage(kwargs.get("model", ""), resp)
    return resp


def emb_model() -> str:
    return _resolve("EMB")[0]


def probe_url() -> str:
    """Host for the scheduler's connectivity probe.

    Probing the provider we actually call is the point: a run deferred because
    api.mistral.ai is unreachable is meaningless once LLM_PROVIDER=azure. The
    scheduler treats any HTTP response — 401/404 included — as online, which
    is what an API root returns to an unauthenticated GET.

    PIPELINE_PROBE_URL overrides everything; a provider whose endpoint is not
    configured falls back to a host that is merely reachable, since the probe
    only has to answer "is there internet".
    """
    override = os.getenv("PIPELINE_PROBE_URL", "").strip()
    if override:
        return override
    if LLM_PROVIDER == "azure" and AZURE_ENDPOINT:
        return AZURE_ENDPOINT
    if LLM_PROVIDER == "custom" and CUSTOM_BASE_URL:
        return CUSTOM_BASE_URL
    if LLM_PROVIDER == "mistral":
        return "https://api.mistral.ai/"
    return "https://api.openai.com/"


def embed(client, inputs, model: str | None = None):
    """client.embeddings.create with rate limiting and usage accounting.

    Every embedding call goes through here (scorer batch + single, KB build,
    check_api) so the ledger sees the whole spend, not just the chat half.
    """
    name = model or emb_model()
    rate_limit()
    resp = client.embeddings.create(model=name, input=inputs)
    _record_usage(name, resp, kind="emb")
    return resp
