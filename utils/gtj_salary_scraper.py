"""
utils/gtj_salary_scraper.py — GermanTechJobs.de salary data scraper.

URL format: https://germantechjobs.de/salaries/{role}/{city}/{level}
Levels:     Junior / Regular / Senior
Data type:  BASE SALARY from job postings (EUR gross annual) — not total comp.
Cache:      data/gtj_cache.json, TTL 14 days.

On any fetch/parse failure: returns empty list silently — never blocks salary estimation.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

CACHE_TTL_DAYS = int(os.getenv("GTJ_CACHE_TTL_DAYS", "14"))
_CACHE_FILE    = Path(os.getenv("GTJ_CACHE_PATH", "./data/gtj_cache.json"))
_BASE_URL      = "https://germantechjobs.de/salaries"
_DEBUG_DIR     = _CACHE_FILE.parent / "gtj_debug"

LEVELS = ("Junior", "Regular", "Senior")

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Role mapping (priority order: specific role > generic) ─────────────────────

_ROLE_MAP: list[tuple[list[str], str]] = [
    (["machine.learning", r"\bml\b", r"\bai\b", "mlops", "data scientist",
      "llm", "applied ai", "tensorflow", "pytorch"],                            "Machine-Learning"),
    (["devops", "sre", "site reliability", "platform engineer",
      "infrastructure engineer", "kubernetes", "terraform"],                    "DevOps"),
    (["cloud engineer", "cloud architect", r"\baws\b", r"\bazure\b", r"\bgcp\b"], "Cloud"),
    (["data engineer", "data platform", "analytics engineer"],                  "Data"),
    (["kotlin"],                                                                 "Kotlin"),
    (["backend", "back.end", "api engineer"],                                   "Backend"),
    (["fullstack", "full.stack"],                                               "Fullstack"),
    (["frontend", "front.end"],                                                 "Frontend"),
    (["java developer", "java engineer", r"\bjava\b"],                          "Java"),
    (["python developer", "python engineer", r"\bpython\b"],                   "Python"),
    (["node.js", "nodejs", r"\bnode\b"],                                       "NodeJS"),
    (["typescript"],                                                             "TypeScript"),
]
_ROLE_FALLBACK = "all"


def _role_slug(title: str) -> str:
    t = title.lower()
    for keywords, slug in _ROLE_MAP:
        if any(re.search(kw, t) for kw in keywords):
            return slug
    return _ROLE_FALLBACK


# ── City mapping ───────────────────────────────────────────────────────────────

_CITY_MAP: list[tuple[list[str], str]] = [
    (["hamburg"],                                    "Hamburg"),
    (["berlin"],                                     "Berlin"),
    (["munich", r"m.nchen"],                         "Munich"),
    (["frankfurt"],                                  "Frankfurt"),
    (["cologne", r"k.ln"],                           "Cologne"),
    (["stuttgart"],                                  "Stuttgart"),
    (["d.sseldorf"],                                 "Dusseldorf"),
    (["bremen"],                                     "Bremen"),
    (["hanover", "hannover"],                        "Hanover"),
    (["nuremberg", r"n.rnberg"],                    "Nuremberg"),
    (["remote", "anywhere", "distributed",
      "worldwide", "globally"],                      "remote"),
]
_CITY_FALLBACK = "all"
_REMOTE_KEYWORDS = ["remote", "anywhere", "distributed", "worldwide", "globally"]


def _city_slug(location: str | None, contract_type: str | None = None) -> str:
    loc = (location or "").lower()
    ct  = (contract_type or "").lower()
    if "remote" in ct or any(kw in loc for kw in _REMOTE_KEYWORDS):
        return "remote"
    for keywords, slug in _CITY_MAP:
        if any(re.search(kw, loc) for kw in keywords):
            return slug
    return _CITY_FALLBACK


# ── Cache ──────────────────────────────────────────────────────────────────────

def _cache_key(role: str, city: str, level: str) -> str:
    return f"{role}__{city}__{level}"


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
        _CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log.warning("gtj_salary_scraper: failed to write cache: %s", exc)


def _is_fresh(entry: dict) -> bool:
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])).days
        return age < CACHE_TTL_DAYS
    except Exception:
        return False


# ── Number parser ──────────────────────────────────────────────────────────────

def _parse_amount(text: str) -> int | None:
    """
    Parse salary amounts in German or English Euro format.
    Handles: "62.500 €", "€62,500", "62.5K", "100.500", "€75,500+"
    German convention: period = thousands separator, comma = decimal.
    """
    t = re.sub(r'[€$+\s ]', '', text)   # strip currency symbols, spaces, +

    # K-suffix: "62.5K" or "75,5K" or "100.5K"
    m = re.fullmatch(r'(\d+)[.,](\d)K', t, re.IGNORECASE)
    if m:
        return round((int(m.group(1)) + int(m.group(2)) / 10) * 1000)
    m = re.fullmatch(r'(\d+)K', t, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000

    # German thousands: "62.500" or "100.500"
    m = re.fullmatch(r'(\d{2,3})\.(\d{3})', t)
    if m:
        return int(m.group(1) + m.group(2))

    # English thousands: "62,500"
    m = re.fullmatch(r'(\d{2,3}),(\d{3})', t)
    if m:
        return int(m.group(1) + m.group(2))

    # Plain integer: "62500"
    m = re.fullmatch(r'(\d{4,6})', t)
    if m:
        return int(m.group(1))

    return None


# ── Page parser ────────────────────────────────────────────────────────────────

# Page text is in German. Label patterns extracted from actual page text:
#   "oberen 10% des Marktes   75.000 €"
#   "oberen 25% des Marktes   67.500 €"
#   "Median Gehalt             60.000 €"
#   "unteren 25% des Marktes  52.500 €"
#   "unteren 10% des Marktes  47.000 €"
#   "Durchschnitt...beträgt 59.800 €"
_AMT = r'([\d.]+\s*€)'   # matches "75.000 €" or "60.000 €"

_PATTERNS: list[tuple[str, str]] = [
    ("p90",    r'oberen\s+10%[^\d€]{0,40}' + _AMT),
    ("p75",    r'oberen\s+25%[^\d€]{0,40}' + _AMT),
    ("median", r'Median\s+Gehalt[^\d€]{0,40}' + _AMT),
    ("average",r'betr[äa]gt\s+' + _AMT),
    ("p25",    r'unteren\s+25%[^\d€]{0,40}' + _AMT),
    ("p10",    r'unteren\s+10%[^\d€]{0,40}' + _AMT),
]


def _parse_page(text: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {k: None for k, _ in _PATTERNS}

    text = re.sub(r'\s+', ' ', text)

    for key, pattern in _PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = _parse_amount(m.group(1))
            if val and 10_000 < val < 300_000:
                result[key] = val

    return result


# ── Playwright fetch ───────────────────────────────────────────────────────────

def _fetch_text(role: str, city: str, level: str) -> str | None:
    """Fetch rendered page text via Playwright. Returns None on any failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("gtj_salary_scraper: playwright not installed")
        return None

    url = f"{_BASE_URL}/{role}/{city}/{level}"
    log.info("gtj_salary_scraper: fetching %s", url)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx     = browser.new_context(user_agent=_USER_AGENT, locale="en-US")
            page    = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            time.sleep(random.uniform(0.5, 1.2))
            text = page.inner_text("body")
            browser.close()

        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (_DEBUG_DIR / f"{role}__{city}__{level}.txt").write_text(text, encoding="utf-8")
        log.debug("gtj_salary_scraper: page text saved (%d chars)", len(text))
        return text
    except Exception as exc:
        log.warning("gtj_salary_scraper: playwright failed %s/%s/%s — %s", role, city, level, exc)
        return None


def _is_usable(salary: dict) -> bool:
    return bool(salary.get("median"))


# ── Public API ─────────────────────────────────────────────────────────────────

REFRESH_ROLES  = ["Backend", "DevOps", "Cloud", "Machine-Learning", "Data", "Java", "Python", "Fullstack"]
REFRESH_CITIES = ["Hamburg", "Berlin", "Munich", "Frankfurt"]


def refresh_gtj_cache() -> int:
    """
    Pre-fetch GTJ salary data for priority roles × cities × all levels.
    Clears stale entries first, then fetches fresh data.
    Returns the number of successfully fetched entries.
    """
    cache = _load_cache()
    for role in REFRESH_ROLES:
        for city in REFRESH_CITIES:
            for level in LEVELS:
                cache.pop(_cache_key(role, city, level), None)
    _save_cache(cache)

    fetched = 0
    for role in REFRESH_ROLES:
        for city in REFRESH_CITIES:
            for level in LEVELS:
                text   = _fetch_text(role, city, level)
                salary = _parse_page(text) if text else {}
                now    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                cache[_cache_key(role, city, level)] = {
                    "role": role, "city": city, "level": level,
                    "salary": salary, "fetched_at": now,
                }
                if _is_usable(salary):
                    fetched += 1
                    log.info("gtj refresh: %s/%s/%s median=%s", role, city, level, salary.get("median"))
    _save_cache(cache)
    log.info("gtj_salary_scraper: refresh complete — %d/%d entries usable", fetched, len(REFRESH_ROLES) * len(REFRESH_CITIES) * len(LEVELS))
    return fetched


def fetch_gtj_data(
    job_title:     str,
    job_location:  str | None = None,
    contract_type: str | None = None,
) -> list[dict]:
    """
    Fetch GTJ base salary data for Junior / Regular / Senior.
    Returns a list of records (one per level with usable data).
    Never raises — returns [] on failure.
    """
    role  = _role_slug(job_title)
    city  = _city_slug(job_location, contract_type)
    cache = _load_cache()
    results: list[dict] = []

    for level in LEVELS:
        key   = _cache_key(role, city, level)
        entry = cache.get(key)

        if entry and _is_fresh(entry):
            log.debug("gtj_salary_scraper: cache hit %s", key)
            if _is_usable(entry.get("salary", {})):
                results.append(entry)
            continue

        text   = _fetch_text(role, city, level)
        salary = _parse_page(text) if text else {}
        now    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        record: dict = {
            "role":       role,
            "city":       city,
            "level":      level,
            "salary":     salary,
            "fetched_at": now,
        }
        cache[key] = record
        log.info(
            "gtj_salary_scraper: %s/%s/%s → median=%s p75=%s p90=%s",
            role, city, level,
            salary.get("median"), salary.get("p75"), salary.get("p90"),
        )
        if _is_usable(salary):
            results.append(record)

    _save_cache(cache)
    if not results:
        log.info("gtj_salary_scraper: no usable data for %s/%s", role, city)
    return results
