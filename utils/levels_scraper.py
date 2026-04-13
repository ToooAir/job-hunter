"""
utils/levels_scraper.py — Levels.fyi salary data scraper with fallback chain + cache.

Scrapes the *aggregate summary statistics* that Levels.fyi computes from their full
dataset (median, percentiles, sample size) — NOT individual data rows.
This avoids the n<100 reliability problem of self-computed averages.

Fetch flow:
  1. Derive location_slug from job's location field (or HOME_COUNTRY env var for remote)
  2. Map job title to Levels.fyi role slug
  3. Try fallback chain: specific_country → europe → global
  4. Stop at first layer that returns a usable summary (has median_total)
  5. If data is in USD, convert to EUR via live FX rate (frankfurter.app → open.er-api.com
     → hardcoded fallback). FX rate is cached with the same TTL as salary data.
  6. Cache each (role_slug, location_slug) independently for CACHE_TTL_DAYS days.

On any scraping failure: return empty result silently — never block salary estimation.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)

# ── Config (overridable via env) ───────────────────────────────────────────────

HOME_COUNTRY         = os.getenv("HOME_COUNTRY", "germany")
CACHE_TTL_DAYS       = int(os.getenv("LEVELS_CACHE_TTL_DAYS", "7"))
FALLBACK_USD_EUR     = float(os.getenv("FALLBACK_USD_EUR_RATE", "0.92"))
_CACHE_FILE    = Path(os.getenv("LEVELS_CACHE_PATH", "./data/levels_cache.json"))

# ── Types ──────────────────────────────────────────────────────────────────────

class LevelsSummary(TypedDict):
    median_total: int | None   # median total comp  (EUR after conversion)
    p25:          int | None   # 25th percentile total comp (EUR after conversion)
    p75:          int | None   # 75th percentile total comp (EUR after conversion)
    p90:          int | None   # 90th percentile total comp (EUR after conversion)
    sample_size:  int | None   # sample count if shown on page
    currency:     str          # "USD" / "EUR" / "unknown" — original page currency
    fx_note:      str          # conversion note for prompt


class LevelsResult(TypedDict):
    summary:      LevelsSummary | None
    source_slug:  str          # actual location slug used
    fetched_at:   str          # ISO-8601


_EMPTY_SUMMARY: LevelsSummary = {
    "median_total": None,
    "p25": None, "p75": None, "p90": None,
    "sample_size": None, "currency": "unknown", "fx_note": "",
}
_EMPTY_RESULT: LevelsResult = {"summary": None, "source_slug": "", "fetched_at": ""}

# ── Role keyword → Levels.fyi slug ────────────────────────────────────────────

_ROLE_MAP: list[tuple[list[str], str]] = [
    (["data engineer", "data platform", "analytics engineer"],              "data-engineer"),
    (["data scientist", "machine learning", r"\bml\b", "mlops", r"\bai\b"], "data-scientist"),
    (["devops", "sre", "site reliability", "platform engineer",
      "infrastructure", "cloud engineer"],                                  "devops-engineer"),
    (["product manager", r"\bpm\b", "product owner"],                       "product-manager"),
    (["backend", "front.end", "frontend", "full.stack", "fullstack",
      "software engineer", "software developer", "web developer"],          "software-engineer"),
]
_ROLE_FALLBACK = "software-engineer"


def _role_slug(title: str) -> str:
    t = title.lower()
    for keywords, slug in _ROLE_MAP:
        if any(re.search(kw, t) for kw in keywords):
            return slug
    return _ROLE_FALLBACK


# ── Location keyword → Levels.fyi slug ────────────────────────────────────────

_LOCATION_MAP: list[tuple[list[str], str]] = [
    (["germany", "deutschland", "berlin", "hamburg", "munich", "münchen",
      "frankfurt", "cologne", "köln", "stuttgart", "düsseldorf"],          "germany"),
    (["netherlands", "nederland", "amsterdam", "rotterdam", "eindhoven"],   "netherlands"),
    (["austria", "österreich", "vienna", "wien", "graz"],                   "austria"),
    (["switzerland", "schweiz", "zurich", "zürich", "basel", "geneva"],     "switzerland"),
    (["france", "paris", "lyon", "toulouse"],                               "france"),
    (["united kingdom", "uk", r"\bgb\b", "london", "manchester",
      "edinburgh", "bristol"],                                               "united-kingdom"),
    (["sweden", "stockholm", "gothenburg"],                                  "sweden"),
    (["poland", "warsaw", "krakow", "wroclaw"],                              "poland"),
    (["spain", "madrid", "barcelona"],                                       "spain"),
    (["portugal", "lisbon", "porto"],                                        "portugal"),
    (["ireland", "dublin"],                                                  "ireland"),
    (["denmark", "copenhagen"],                                              "denmark"),
    (["finland", "helsinki"],                                                "finland"),
    (["norway", "oslo"],                                                     "norway"),
    (["czech", "prague", "brno"],                                            "czech-republic"),
]
_REMOTE_KEYWORDS = ["remote", "anywhere", "distributed", "worldwide", "globally"]

_FALLBACK_CHAIN: dict[str, list[str]] = {
    slug: [slug, "global"]
    for _, slug in _LOCATION_MAP
}
_FALLBACK_CHAIN["global"] = ["global"]

# Public alias used by dashboard
FALLBACK_CHAIN = _FALLBACK_CHAIN


def _location_slug(location: str | None, contract_type: str | None) -> str:
    loc_lower = (location or "").lower()
    ct_lower  = (contract_type or "").lower()
    if "remote" in ct_lower or any(kw in loc_lower for kw in _REMOTE_KEYWORDS):
        return HOME_COUNTRY
    for keywords, slug in _LOCATION_MAP:
        if any(re.search(kw, loc_lower) for kw in keywords):
            return slug
    log.debug("levels_scraper: could not map location '%s', using HOME_COUNTRY", location)
    return HOME_COUNTRY


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_key(role_slug: str, location_slug: str) -> str:
    return f"{role_slug}__{location_slug}"


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("levels_scraper: failed to write cache: %s", exc)


def _is_fresh(entry: dict) -> bool:
    try:
        fetched  = datetime.fromisoformat(entry["fetched_at"])
        age_days = (datetime.now(timezone.utc) - fetched).days
        return age_days < CACHE_TTL_DAYS
    except Exception:
        return False


def _summary_usable(result: LevelsResult) -> bool:
    s = result.get("summary")
    return bool(s and s.get("median_total"))


def _cache_get(role_slug: str, location_slug: str) -> LevelsResult | None:
    cache = _load_cache()
    key   = _cache_key(role_slug, location_slug)
    entry = cache.get(key)
    if entry and _is_fresh(entry):
        log.debug("levels_scraper: cache hit %s", key)
        return LevelsResult(**entry)
    return None


def _cache_set(role_slug: str, location_slug: str, result: LevelsResult) -> None:
    cache = _load_cache()
    cache[_cache_key(role_slug, location_slug)] = result
    _save_cache(cache)


def clear_cache(role_slug: str, location_slug: str) -> None:
    """Remove one cache entry (used by dashboard refresh button)."""
    cache = _load_cache()
    key   = _cache_key(role_slug, location_slug)
    if key in cache:
        del cache[key]
        _save_cache(cache)
        log.info("levels_scraper: cache cleared for %s", key)


# ── FX rate (USD → EUR) ────────────────────────────────────────────────────────

_FX_CACHE_KEY = "__fx_usd_eur"

_FX_SOURCES = [
    # (url, json_path_to_rate)
    ("https://api.frankfurter.app/latest?from=USD&to=EUR", ("rates", "EUR")),
    ("https://open.er-api.com/v6/latest/USD",              ("rates", "EUR")),
]


def _fetch_fx_live() -> float | None:
    """Try each FX source in order. Return rate or None if all fail."""
    for url, path in _FX_SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            rate = data
            for key in path:
                rate = rate[key]
            rate = float(rate)
            log.info("levels_scraper: FX USD→EUR = %.4f (from %s)", rate, url)
            return rate
        except Exception as exc:
            log.warning("levels_scraper: FX source %s failed: %s", url, exc)
    return None


def _get_usd_eur_rate() -> tuple[float, str]:
    """
    Return (rate, note) for USD→EUR conversion.
    Tries: live API → same-TTL cache → hardcoded fallback.
    note describes the source for prompt transparency.
    """
    cache     = _load_cache()
    fx_entry  = cache.get(_FX_CACHE_KEY, {})
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Cache hit (within TTL)
    if fx_entry and _is_fresh(fx_entry):
        rate = fx_entry["rate"]
        date = fx_entry.get("date", "cached")
        log.debug("levels_scraper: FX cache hit %.4f (%s)", rate, date)
        return rate, f"at {rate:.4f} ({date})"

    # Try live sources
    live_rate = _fetch_fx_live()
    if live_rate is not None:
        cache[_FX_CACHE_KEY] = {
            "rate":       live_rate,
            "date":       today_str,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _save_cache(cache)
        return live_rate, f"at {live_rate:.4f} ({today_str})"

    # Stale cache fallback (better than hardcoded if available)
    if fx_entry and fx_entry.get("rate"):
        rate = fx_entry["rate"]
        date = fx_entry.get("date", "stale")
        log.warning("levels_scraper: using stale FX cache %.4f (%s)", rate, date)
        return rate, f"at {rate:.4f} ({date}, stale)"

    # Last resort: hardcoded env value
    log.warning("levels_scraper: using fallback FX rate %.4f", FALLBACK_USD_EUR)
    return FALLBACK_USD_EUR, f"at {FALLBACK_USD_EUR:.2f} (fallback — verify current rate)"


def _apply_usd_to_eur(summary: LevelsSummary) -> LevelsSummary:
    """Convert all monetary fields from USD to EUR in-place. Returns mutated dict."""
    if summary.get("currency") != "USD":
        return summary

    rate, note = _get_usd_eur_rate()
    for field in ("median_total", "p25", "p75", "p90"):
        if summary.get(field):
            summary[field] = round(summary[field] * rate)  # type: ignore[operator]

    summary["fx_note"] = f"converted from USD {note}"
    return summary


# ── Page summary parser ────────────────────────────────────────────────────────

def _parse_amount(text: str) -> int | None:
    """Parse '$120K', '€95,000', '120k', '95000' → int. Returns None on failure."""
    t = text.replace(",", "").replace(" ", "")
    m = re.search(r"[\$€]?(\d+\.?\d*)[Kk]", t)
    if m:
        return round(float(m.group(1)) * 1000)
    m = re.search(r"[\$€]?(\d{4,7})", t)
    if m:
        return int(m.group(1))
    return None


def _parse_summary_from_text(full_text: str) -> LevelsSummary:
    """
    Extract aggregate stats from Levels.fyi page text.

    Actual page structure (value appears BEFORE its label):
        €82,394
        MEDIAN TOTAL COMP
        €68.4K
        25TH%
        €100K
        75TH%
        €124K
        90TH%

    Also parses FAQ sentences as secondary source:
        "The average total compensation ... is €82,394."
    """
    summary = dict(_EMPTY_SUMMARY)

    # ── Detect currency ───────────────────────────────────────────────────────
    if re.search(r"€\s*\d", full_text):
        summary["currency"] = "EUR"
    elif re.search(r"\$\s*\d", full_text):
        summary["currency"] = "USD"

    # ── Primary: value-before-label patterns (stats box) ─────────────────────
    # Label follows the amount, separated by whitespace / newlines
    _AMT = r"([\$€][\d,\.]+[Kk]?)"   # e.g. €82,394  €68.4K  $120K
    _BEFORE: list[tuple[str, str]] = [
        ("median_total", _AMT + r"[\s\n]+MEDIAN TOTAL COMP"),
        ("p25",          _AMT + r"[\s\n]+25TH%"),
        ("p75",          _AMT + r"[\s\n]+75TH%"),
        ("p90",          _AMT + r"[\s\n]+90TH%"),
    ]

    for key, pattern in _BEFORE:
        m = re.search(pattern, full_text, re.IGNORECASE)
        if m:
            val = _parse_amount(m.group(1))
            if val:
                summary[key] = val

    # ── Secondary: FAQ / prose sentences (label before value) ────────────────
    _AFTER: list[tuple[str, str]] = [
        ("median_total", r"average total compensation[^\n]{0,60}?([\$€][\d,\.]+)"),
        ("sample_size",  r"based on (\d[\d,]*)\s+(?:salaries|data points|responses)"),
        ("sample_size",  r"(\d[\d,]*)\s+(?:salaries|data points)\s+submitted"),
    ]

    for key, pattern in _AFTER:
        if summary.get(key):
            continue   # already found from primary
        m = re.search(pattern, full_text, re.IGNORECASE)
        if not m:
            continue
        if key == "sample_size":
            try:
                summary[key] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass
        else:
            val = _parse_amount(m.group(1))
            if val:
                summary[key] = val

    summary["fx_note"] = ""
    return LevelsSummary(**summary)


# ── Playwright scraper ─────────────────────────────────────────────────────────

def _scrape_summary(role_slug: str, location_slug: str) -> LevelsSummary:
    """
    Scrape Levels.fyi summary stats for (role, location).
    Targets the page-level aggregate (median, percentiles, sample size)
    that Levels.fyi computes from their full dataset.
    Returns _EMPTY_SUMMARY on any failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("levels_scraper: playwright not available")
        return _EMPTY_SUMMARY

    url = (
        f"https://www.levels.fyi/t/{role_slug}/locations/{location_slug}"
        if location_slug != "global"
        else f"https://www.levels.fyi/t/{role_slug}/"
    )
    log.info("levels_scraper: fetching %s", url)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx     = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            time.sleep(random.uniform(0.5, 1.5))

            full_text = page.inner_text("body")
            browser.close()

        # Always dump raw page text for regex debugging / maintenance
        _debug_dir = _CACHE_FILE.parent / "levels_debug"
        _debug_dir.mkdir(parents=True, exist_ok=True)
        (_debug_dir / f"{role_slug}__{location_slug}.txt").write_text(
            full_text, encoding="utf-8"
        )
        log.info("levels_scraper: raw text saved to %s", _debug_dir / f"{role_slug}__{location_slug}.txt")

        summary = _parse_summary_from_text(full_text)
        summary = _apply_usd_to_eur(summary)
        log.info(
            "levels_scraper: parsed summary for %s/%s — total=%s p25=%s p75=%s n=%s currency=%s fx=%r",
            role_slug, location_slug,
            summary["median_total"], summary["p25"], summary["p75"],
            summary["sample_size"], summary["currency"], summary["fx_note"],
        )
        return summary

    except Exception as exc:
        log.warning("levels_scraper: scraping failed for %s/%s: %s", role_slug, location_slug, exc)
        return _EMPTY_SUMMARY


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_levels_data(
    job_title:     str,
    job_location:  str | None = None,
    contract_type: str | None = None,
) -> list[LevelsResult]:
    """
    Main entry point. Fetches ALL layers of the fallback chain so the prompt
    can include a multi-location comparison table.

    Returns a list of LevelsResult (one per layer with usable data).
    Never raises — returns [] if all layers fail.
    """
    role_slug  = _role_slug(job_title)
    start_slug = _location_slug(job_location, contract_type)
    chain      = _FALLBACK_CHAIN.get(start_slug, [start_slug, "global"])

    results: list[LevelsResult] = []
    for loc_slug in chain:
        cached = _cache_get(role_slug, loc_slug)
        if cached is not None:
            if _summary_usable(cached):
                results.append(cached)
            continue

        summary = _scrape_summary(role_slug, loc_slug)
        result: LevelsResult = {
            "summary":     summary,
            "source_slug": loc_slug,
            "fetched_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _cache_set(role_slug, loc_slug, result)
        if _summary_usable(result):
            results.append(result)

    if not results:
        log.info("levels_scraper: no usable data for role=%s location=%s", role_slug, start_slug)
    return results


def fetch_levels_by_slug(role_slug: str, location_slug: str) -> list[LevelsResult]:
    """
    Like fetch_levels_data() but accepts pre-resolved slugs directly.
    Used by the dashboard "Refresh All" button to iterate all role slugs.
    """
    chain = _FALLBACK_CHAIN.get(location_slug, [location_slug, "global"])

    results: list[LevelsResult] = []
    for loc_slug in chain:
        cached = _cache_get(role_slug, loc_slug)
        if cached is not None:
            if _summary_usable(cached):
                results.append(cached)
            continue

        summary = _scrape_summary(role_slug, loc_slug)
        result: LevelsResult = {
            "summary":     summary,
            "source_slug": loc_slug,
            "fetched_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _cache_set(role_slug, loc_slug, result)
        if _summary_usable(result):
            results.append(result)

    if not results:
        log.info("levels_scraper: no usable data for role=%s location=%s", role_slug, location_slug)
    return results


def cache_info(job_title: str, job_location: str | None, contract_type: str | None) -> dict | None:
    """
    Return cache metadata for the UI freshness indicator.
    Reports the oldest fetched_at across all cached layers (worst-case freshness).
    Returns None if no usable cache exists for any layer.
    """
    role_slug  = _role_slug(job_title)
    start_slug = _location_slug(job_location, contract_type)
    chain      = _FALLBACK_CHAIN.get(start_slug, [start_slug, "global"])
    cache      = _load_cache()

    layers: list[dict] = []
    for loc_slug in chain:
        key   = _cache_key(role_slug, loc_slug)
        entry = cache.get(key)
        if entry and entry.get("summary") and entry["summary"].get("median_total"):
            layers.append({
                "location_slug": loc_slug,
                "fetched_at":    entry.get("fetched_at", ""),
                "is_fresh":      _is_fresh(entry),
            })

    if not layers:
        return None

    oldest = min(layers, key=lambda x: x["fetched_at"])
    return {
        "role_slug":      role_slug,
        "location_slugs": [l["location_slug"] for l in layers],
        "fetched_at":     oldest["fetched_at"],
        "is_fresh":       all(l["is_fresh"] for l in layers),
        "layer_count":    len(layers),
    }
