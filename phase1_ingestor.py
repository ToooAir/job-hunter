"""
Phase 1 — Job Ingestor
Scrapes arbeitnow, englishjobs.de, and germantechjobs.de,
then upserts raw job listings into the SQLite database.
"""

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlencode

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.db import init_db, upsert_job

# ── Setup ──────────────────────────────────────────────────────────────────────

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "./data/jobs.db")
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── HTML Selectors（集中管理，改版時只需修改此處）─────────────────────────────

ENGLISHJOBS_SELECTORS = {
    # 列表頁：職缺卡片容器（實測確認：class="job js-job"）
    "card":           "div.js-job",
    # 卡片 id 屬性 = clickout id；/clickout/{id} 不需要 sig 就能跳轉到雇主頁面
    # 注意：舊的 /jobs/{id} 格式不存在，只會顯示「No jobs available」
    "clickout_tpl":   "https://englishjobs.de/clickout/{job_id}",
    "title":          "h3",
    # company = ul > li[0]，location = ul > li[1]
}

GERMANTECHJOBS_SELECTORS = {
    # JS 渲染 SPA，requests 無法取得內容，保留供日後 Playwright 使用
    "disabled":       True,
    "card_primary":   "div.job-card, li.job-listing, article",
    "detail_href":    "/jobs/",
    "jd_classes":     ["description", "content"],
    "base_domain":    "https://germantechjobs.de",
}

RELOCATEME_SELECTORS = {
    # 列表頁（實測確認）
    "card":           "div.jobs-list__job",
    "title":          ".job__title a",
    # .job__company 有兩個 div：[0]=國家/城市, [1]=公司名
    "company_divs":   ".job__company",
    "base_domain":    "https://relocate.me",
    # 詳情頁 JD 容器（依序嘗試）
    "jd_classes":     ["job-content", "large-9"],
}

# ── Utility functions ──────────────────────────────────────────────────────────


def make_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _url_in_db(conn, url: str) -> bool:
    """Return True if a job with this URL is already in the DB (cheap pre-fetch check)."""
    return conn.execute(
        "SELECT 1 FROM jobs WHERE id = ?", (make_id(url),)
    ).fetchone() is not None


def clean_html(html_str: str) -> str:
    return BeautifulSoup(html_str, "html.parser").get_text(
        separator="\n", strip=True
    )


def safe_get(url: str, log_404: bool = True, **kwargs) -> requests.Response | None:
    try:
        resp = requests.get(url, timeout=10, headers=HEADERS, **kwargs)
        if resp.status_code == 404 and not log_404:
            return None
        resp.raise_for_status()
        time.sleep(1.2)
        return resp
    except Exception as exc:
        log.warning("GET failed: %s — %s", url, exc)
        return None


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _warn_empty_jd(record: dict) -> None:
    jd_len = len(record.get("raw_jd_text") or "")
    if jd_len < 100:
        log.warning("short/empty JD (%d chars): %s @ %s [%s]",
                    jd_len, record.get("title"), record.get("company"), record.get("source"))


def expiry(days: int) -> str:
    """Return an ISO-8601 UTC timestamp `days` from now — used as expires_at."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


# ── Source 1: Arbeitnow ────────────────────────────────────────────────────────


def scrape_arbeitnow(
    conn,
    keywords: list[str],
    locations: list[str],
    remote_filter: bool,
    max_pages: int,
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]
    loc_lower = [l.lower() for l in locations]

    for page in range(1, max_pages + 1):
        url = f"https://www.arbeitnow.com/api/job-board-api?page={page}"
        resp = safe_get(url)
        if resp is None:
            break

        data = resp.json()
        jobs = data.get("data", [])
        if not jobs:
            break

        for job in jobs:
            title = job.get("title", "")
            location = job.get("location", "") or ""
            is_remote = job.get("remote", False)

            title_match = any(kw in title.lower() for kw in kw_lower)
            loc_match = any(loc in location.lower() for loc in loc_lower)
            remote_match = remote_filter and is_remote

            if not title_match:
                continue
            if not (loc_match or remote_match):
                continue

            job_url = job.get("url", "")
            if not job_url:
                continue

            record = {
                "id":          make_id(job_url),
                "company":     job.get("company_name", ""),
                "title":       title,
                "url":         job_url,
                "source":      "arbeitnow",
                "source_tier": "auto",
                "location":    location,
                "raw_jd_text": clean_html(job.get("description", "")),
                "fetched_at":  utcnow(),
                "expires_at":  job.get("deadline") or expiry(45),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

        log.info("arbeitnow page %d: %d total so far", page, added + skipped)

        # Stop early if last page returned fewer than 25 results
        if len(jobs) < 25:
            break

    return added, skipped


# ── Source 2: EnglishJobs.de ───────────────────────────────────────────────────


def _parse_englishjobs_listing(soup: BeautifulSoup, base: str) -> list[dict]:
    """Extract job cards from an englishjobs.de listing page."""
    sel = ENGLISHJOBS_SELECTORS
    cards = []

    for card in soup.select(sel["card"]):
        job_id = card.get("id", "")
        if not job_id:
            continue
        # Stable clickout URL — works without signature, redirects to employer page
        clickout_url = sel["clickout_tpl"].format(job_id=job_id)

        title_el = card.select_one(sel["title"])
        title = title_el.get_text(strip=True) if title_el else ""

        lis = card.select("ul li")
        company  = lis[0].get_text(strip=True) if len(lis) > 0 else ""
        location = lis[1].get_text(strip=True) if len(lis) > 1 else ""

        cards.append({"title": title, "company": company, "location": location, "clickout_url": clickout_url})
    return cards


def scrape_englishjobs(
    conn,
    base_url: str,
    keywords: list[str],
    max_pages: int,
) -> tuple[int, int]:
    added = skipped = 0
    seen_urls: set[str] = set()

    for keyword in keywords:
        for page in range(1, max_pages + 1):
            params = {"q": keyword, "page": page}
            search_url = f"{base_url}?{urlencode(params)}"
            resp = safe_get(search_url)
            if resp is None:
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = _parse_englishjobs_listing(soup, base_url)
            if not cards:
                if page == 1:
                    log.warning("englishjobs kw=%r: page 1 returned 0 cards — selector may be broken (url=%s)", keyword, search_url)
                break

            for card in cards:
                clickout_url = card["clickout_url"]
                if clickout_url in seen_urls:
                    skipped += 1
                    continue
                seen_urls.add(clickout_url)

                # Pre-check: clickout URL is the stable key for dedup
                if _url_in_db(conn, clickout_url):
                    skipped += 1
                    continue

                # Follow redirect chain: clickout → talent.com → employer ATS page
                detail_resp = safe_get(clickout_url)
                if detail_resp is None:
                    continue

                # Use the final URL (after all redirects) as the canonical job URL
                canonical_url = detail_resp.url

                detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                # Parse JD from the employer's actual page — try common content containers
                jd_el = (
                    detail_soup.select_one("main")
                    or detail_soup.select_one("article")
                    or detail_soup.select_one("[class*='content']")
                    or detail_soup.body
                )
                raw_jd = jd_el.get_text(separator="\n", strip=True) if jd_el else ""

                record = {
                    "id":          make_id(clickout_url),
                    "company":     card["company"],
                    "title":       card["title"],
                    "url":         canonical_url,
                    "source":      "englishjobs",
                    "source_tier": "auto",
                    "location":    card["location"],
                    "raw_jd_text": raw_jd,
                    "fetched_at":  utcnow(),
                    "expires_at":  expiry(45),
                    "status":      "un-scored",
                }

                _warn_empty_jd(record)
                if upsert_job(conn, record):
                    added += 1
                else:
                    skipped += 1

            log.info(
                "englishjobs kw=%r page=%d: added=%d skipped=%d",
                keyword, page, added, skipped,
            )

    return added, skipped


# ── Source 3: GermanTechJobs ───────────────────────────────────────────────────
#
# Previously skipped (JS SPA). Now uses:
#   Step 1 — requests: GET https://germantechjobs.de/api/jobsLight
#             Returns all 3000+ listings with title/company/city/technologies fields.
#             No auth, no rate limit observed.
#   Step 2 — Playwright (headless Chromium): render detail page per matched job
#             JD selector: [class*="job-detail"] (confirmed ~5 KB HTML)
#             URL: https://germantechjobs.de/jobs/{jobUrl}
#
# Filter logic (listing stage, no Playwright needed):
#   - keyword match: job name OR technologies list
#   - location match: cityCategory OR workplace=remote
#   - language: "English" OR keyword forces English context

GTJ_API = "https://germantechjobs.de/api/jobsLight"
GTJ_BASE = "https://germantechjobs.de"
GTJ_TARGET_CITIES = {
    "Hamburg", "Berlin", "Munich", "Frankfurt", "Cologne", "Dusseldorf",
    "Stuttgart", "Hanover", "Bremen", "Nuremberg", "Dresden", "Leipzig",
    "Karlsruhe", "Dortmund", "Essen", "Heidelberg", "Mannheim", "Bonn",
}


def _gtj_fetch_listing() -> list[dict] | None:
    resp = safe_get(GTJ_API)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:
        log.warning("germantechjobs: JSON parse error — %s", exc)
        return None


def _gtj_fetch_jd_playwright(job_urls: list[str]) -> dict[str, str]:
    """Batch-fetch JDs via Playwright. Returns {jobUrl: jd_text}, or {} if unavailable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("germantechjobs: playwright not installed — skipping JD fetch")
        return {}

    results: dict[str, str] = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({"User-Agent": HEADERS["User-Agent"]})

            for job_url_slug in job_urls:
                url = f"{GTJ_BASE}/jobs/{job_url_slug}"
                try:
                    page.goto(url, wait_until="networkidle", timeout=25000)
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    jd_el = (
                        soup.select_one("[class*='job-detail']")
                        or soup.find("main")
                        or soup.body
                    )
                    jd_text = jd_el.get_text(separator="\n", strip=True) if jd_el else ""
                    results[job_url_slug] = jd_text
                    time.sleep(0.8)
                except Exception as exc:
                    log.warning("germantechjobs: Playwright error for %s — %s", url, exc)
                    results[job_url_slug] = ""

            browser.close()
    except Exception as exc:
        # Browser not installed (playwright install chromium not run) or other launch failure
        log.warning("germantechjobs: Playwright unavailable — %s", exc)
        return {}

    return results


def scrape_germantechjobs(
    conn,
    keywords: list[str],
    target_cities: set[str] | None = None,
    include_remote: bool = True,
    max_detail_fetches: int = 50,
) -> tuple[int, int]:
    """
    Scrape GermanTechJobs via its internal REST API + Playwright for JD.

    Parameters
    ----------
    keywords        : Title/tech keyword filter (case-insensitive).
    target_cities   : cityCategory values to accept (uses GTJ_TARGET_CITIES if None).
    include_remote  : Also accept jobs with workplace='remote'.
    max_detail_fetches : Playwright calls are slow (~1.5s each); cap to avoid long runs.
    """
    if target_cities is None:
        target_cities = GTJ_TARGET_CITIES

    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]

    # ── Step 1: fetch listing ──────────────────────────────────────────────────
    jobs_all = _gtj_fetch_listing()
    if not jobs_all:
        log.warning("germantechjobs: listing fetch failed")
        return 0, 0

    log.info("germantechjobs: %d total listings", len(jobs_all))

    # ── Step 2: filter ────────────────────────────────────────────────────────
    matched = []
    for job in jobs_all:
        city: str = job.get("cityCategory") or ""
        workplace: str = job.get("workplace") or ""
        is_remote = workplace == "remote"

        if not (city in target_cities or (include_remote and is_remote)):
            continue

        name: str = job.get("name") or ""
        techs: list = job.get("technologies") or []
        haystack = (name + " " + " ".join(techs)).lower()
        if not any(kw in haystack for kw in kw_lower):
            continue

        matched.append(job)

    log.info("germantechjobs: %d jobs matched filter", len(matched))

    # Cap Playwright fetches to avoid very long runtimes
    to_fetch = matched[:max_detail_fetches]
    if len(matched) > max_detail_fetches:
        log.warning(
            "germantechjobs: capping Playwright fetches at %d (of %d matched)",
            max_detail_fetches, len(matched),
        )

    # Filter out jobs already in DB — Playwright is expensive, skip known URLs
    pre_filter_count = len(to_fetch)
    to_fetch_new = [
        j for j in to_fetch
        if j.get("jobUrl") and not _url_in_db(conn, f"{GTJ_BASE}/jobs/{j['jobUrl']}")
    ]
    skipped += pre_filter_count - len(to_fetch_new)
    to_fetch = to_fetch_new
    log.info(
        "germantechjobs: %d new (filtered from %d matched, %d already in DB)",
        len(to_fetch), pre_filter_count, pre_filter_count - len(to_fetch),
    )

    # ── Step 3: batch JD via Playwright ───────────────────────────────────────
    job_url_slugs = [j["jobUrl"] for j in to_fetch if j.get("jobUrl")]
    jd_map = _gtj_fetch_jd_playwright(job_url_slugs)

    if not jd_map and job_url_slugs:
        # Playwright completely unavailable — skip upsert to avoid title-only records
        log.warning("germantechjobs: Playwright returned no JDs — skipping upsert")
        return 0, 0

    # ── Step 4: upsert ────────────────────────────────────────────────────────
    for job in to_fetch:
        slug: str = job.get("jobUrl") or ""
        if not slug:
            continue

        job_url = f"{GTJ_BASE}/jobs/{slug}"
        raw_jd = jd_map.get(slug) or job.get("name") or ""
        city: str = job.get("cityCategory") or ""
        workplace: str = job.get("workplace") or ""

        record = {
            "id":          make_id(job_url),
            "company":     job.get("company") or "",
            "title":       job.get("name") or "",
            "url":         job_url,
            "source":      "germantechjobs",
            "source_tier": "auto",
            "location":    city if city else ("Remote" if workplace == "remote" else ""),
            "raw_jd_text": raw_jd,
            "fetched_at":  utcnow(),
            "expires_at":  expiry(45),
            "status":      "un-scored",
        }

        _warn_empty_jd(record)
        if upsert_job(conn, record):
            added += 1
        else:
            skipped += 1

    log.info("germantechjobs: added=%d skipped=%d", added, skipped)
    return added, skipped


# ── Source 4: Bundesagentur für Arbeit ────────────────────────────────────────

BA_API = "https://api.arbeitsagentur.de/jobsuche/v2"
BA_HEADERS = {
    "User-Agent": "Jobsuche/3.0 (https://api.arbeitsagentur.de)",
    "X-API-Key": "jobboerse",
}


def _ba_safe_get(url: str, params: dict) -> dict | None:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, params=params, headers=BA_HEADERS, timeout=10, verify=False)
        resp.raise_for_status()
        time.sleep(1.0)
        return resp.json()
    except Exception as exc:
        log.warning("BA GET failed: %s %s — %s", url, params, exc)
        return None


def _ba_detail(hash_id: str) -> str:
    """Fetch full job description from Bundesagentur detail endpoint."""
    data = _ba_safe_get(f"{BA_API}/jobs/{hash_id}", {})
    if not data:
        return ""
    angebot = data.get("stellenangebot", {})
    return angebot.get("stellenbeschreibung", "") or ""


def scrape_bundesagentur(
    conn,
    keywords: list[str],
    location: str,
    radius_km: int,
    include_remote: bool,
    size: int,
) -> tuple[int, int]:
    added = skipped = 0
    seen_refnr: set[str] = set()

    search_locations = [location]
    if include_remote:
        search_locations.append("Homeoffice")

    for wo in search_locations:
        for keyword in keywords:
            params = {
                "was": keyword,
                "wo": wo,
                "umkreis": radius_km if wo != "Homeoffice" else 0,
                "size": size,
                "page": 0,
            }
            data = _ba_safe_get(f"{BA_API}/jobs", params)
            if not data:
                continue

            jobs = data.get("stellenangebote") or []
            log.info("bundesagentur kw=%r wo=%r: %d Treffer", keyword, wo, len(jobs))

            for job in jobs:
                refnr = job.get("refnr", "")
                if not refnr or refnr in seen_refnr:
                    skipped += 1
                    continue
                seen_refnr.add(refnr)

                job_url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
                hash_id = job.get("hashId", "")
                raw_jd = _ba_detail(hash_id) if hash_id else ""

                arbeitsort = job.get("arbeitsort") or {}
                location_str = ", ".join(
                    filter(None, [arbeitsort.get("ort"), arbeitsort.get("land")])
                )

                record = {
                    "id":          make_id(job_url),
                    "company":     job.get("arbeitgeber", ""),
                    "title":       job.get("titel", ""),
                    "url":         job_url,
                    "source":      "bundesagentur",
                    "source_tier": "auto",
                    "location":    location_str,
                    "raw_jd_text": clean_html(raw_jd) if raw_jd else job.get("titel", ""),
                    "fetched_at":  utcnow(),
                    "expires_at":  expiry(45),
                    "status":      "un-scored",
                }

                _warn_empty_jd(record)
                if upsert_job(conn, record):
                    added += 1
                else:
                    skipped += 1

    return added, skipped


# ── Source 5: WeAreDevelopers ─────────────────────────────────────────────────
#
# Private (undocumented) REST API discovered via browser devtools.
# GET https://wad-api.wearedevelopers.com/api/v2/jobs/search
#   ?q=<keyword>&per_page=100&page=N          → Germany-located jobs
#   ?q=<keyword>&remote=true&per_page=100&page=N → remote jobs
# Max per_page = 200; 500 returns empty.
# Detail endpoint: /v2/jobs/details?job_id=ID&job_slug=SLUG[&external=true]
# job_type="job-listing" → external aggregated job (use external=true for detail)
# job_type="job"         → WAD-native listing (detail needs company_id; skip detail)

WAD_API = "https://wad-api.wearedevelopers.com/api"
WAD_HEADERS = {
    **HEADERS,
    "Origin": "https://www.wearedevelopers.com",
    "Referer": "https://www.wearedevelopers.com/",
}

# Post-filter: keep only jobs whose location mentions one of these
WAD_DE_TERMS = [
    "germany", "deutschland", "hamburg", "berlin", "munich", "münchen",
    "frankfurt", "cologne", "köln", "düsseldorf", "stuttgart", "hannover",
    "leipzig", "dresden", "nuremberg", "nürnberg", "dortmund", "essen",
    "bremen", "heidelberg", "mannheim", "karlsruhe", "bonn",
]


def _wad_safe_get(path: str, params: dict) -> dict | None:
    try:
        resp = requests.get(
            WAD_API + path, params=params, headers=WAD_HEADERS, timeout=10
        )
        resp.raise_for_status()
        time.sleep(0.8)
        return resp.json()
    except Exception as exc:
        log.warning("WAD GET failed: %s %s — %s", path, params, exc)
        return None


def _wad_jd(job_id: int, slug: str, external: bool) -> str:
    """Fetch full JD from detail endpoint. Returns combined HTML-stripped text."""
    params: dict = {"job_id": job_id, "job_slug": slug}
    if external:
        params["external"] = "true"
    data = _wad_safe_get("/v2/jobs/details", params)
    if not data:
        return ""
    parts = [
        data.get("description") or "",
        data.get("candidate_description") or "",
        data.get("conditions_description") or "",
    ]
    return clean_html("\n".join(p for p in parts if p))


def scrape_wearedevelopers(
    conn,
    keywords: list[str],
    max_pages: int,
    per_page: int,
) -> tuple[int, int]:
    added = skipped = 0
    seen_ids: set[int] = set()
    kw_lower = [k.lower() for k in keywords]

    # Two passes: Germany-located jobs + remote jobs
    passes = [
        {"location": "Germany"},
        {"remote": "true"},
    ]

    for base_params in passes:
        pass_label = "Germany" if "location" in base_params else "remote"

        for keyword in keywords:
            for page in range(1, max_pages + 1):
                params = {
                    "q": keyword,
                    "per_page": per_page,
                    "page": page,
                    **base_params,
                }
                data = _wad_safe_get("/v2/jobs/search", params)
                if not data:
                    break

                jobs = data.get("data", [])
                if not jobs:
                    break

                page_added = 0
                for job in jobs:
                    job_id: int = job.get("id", 0)
                    if not job_id or job_id in seen_ids:
                        skipped += 1
                        continue
                    seen_ids.add(job_id)

                    title: str = job.get("title", "")
                    location: str = job.get("location") or ""
                    is_remote: bool = job.get("remote", False)

                    # Post-filter: must be remote OR in a German city
                    loc_lower = location.lower()
                    if not is_remote and not any(t in loc_lower for t in WAD_DE_TERMS):
                        continue

                    # Title must match at least one keyword
                    if not any(kw in title.lower() for kw in kw_lower):
                        continue

                    external = job.get("job_type") == "job-listing"
                    slug: str = job.get("slug", "")

                    # Canonical WAD URL (used as unique key)
                    if external:
                        job_url = f"https://www.wearedevelopers.com/en/jobs/ext/{job_id}/{slug}"
                    else:
                        job_url = f"https://www.wearedevelopers.com/en/jobs/{job_id}/{slug}"

                    # Skip if already in DB — saves external detail API call on daily runs
                    if _url_in_db(conn, job_url):
                        skipped += 1
                        continue

                    # Fetch full JD (external only; native jobs need company_id we don't have)
                    raw_jd = ""
                    if external:
                        raw_jd = _wad_jd(job_id, slug, external=True)
                    if not raw_jd:
                        # Fallback: combine skills list as minimal context
                        skills = job.get("skills", [])
                        raw_jd = f"{title}\n" + " ".join(skills) if skills else title

                    company: str = job.get("company_name", "").strip()

                    record = {
                        "id":          make_id(job_url),
                        "company":     company,
                        "title":       title,
                        "url":         job_url,
                        "source":      "wearedevelopers",
                        "source_tier": "auto",
                        "location":    location or ("Remote" if is_remote else ""),
                        "raw_jd_text": raw_jd,
                        "fetched_at":  utcnow(),
                        "expires_at":  expiry(45),
                        "status":      "un-scored",
                    }

                    _warn_empty_jd(record)
                    if upsert_job(conn, record):
                        added += 1
                        page_added += 1
                    else:
                        skipped += 1

                log.info(
                    "wearedevelopers kw=%r pass=%s page=%d: added=%d skipped=%d",
                    keyword, pass_label, page, added, skipped,
                )

                # Early exit: results are newest-first; 0 new on this page → nothing newer ahead
                if page_added == 0:
                    break

                # No more pages
                pagination = data.get("pagination", {})
                if page >= (pagination.get("last_page") or 1):
                    break

    return added, skipped


# ── Source 6: Remotive ─────────────────────────────────────────────────────────

REMOTIVE_API = "https://remotive.com/api/remote-jobs"


def scrape_remotive(
    conn,
    categories: list[str],
    keywords: list[str],
    limit: int,
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]

    for category in categories:
        resp = safe_get(REMOTIVE_API, params={"category": category, "limit": limit})
        if resp is None:
            continue

        jobs = resp.json().get("jobs", [])
        log.info("remotive category=%r: %d 筆", category, len(jobs))

        for job in jobs:
            # keyword filter against title + tags
            title = job.get("title", "")
            tags  = " ".join(job.get("tags", []))
            haystack = (title + " " + tags).lower()
            if not any(kw in haystack for kw in kw_lower):
                continue

            job_url = job.get("url", "")
            if not job_url:
                continue

            raw_jd = clean_html(job.get("description", ""))
            candidate_location = job.get("candidate_required_location", "") or "Remote"

            record = {
                "id":          make_id(job_url),
                "company":     job.get("company_name", ""),
                "title":       title,
                "url":         job_url,
                "source":      "remotive",
                "source_tier": "auto",
                "location":    candidate_location,
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(60),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    return added, skipped


# ── Source 6: Relocate.me ─────────────────────────────────────────────────────


def _parse_relocateme_listing(soup: BeautifulSoup) -> list[dict]:
    sel = RELOCATEME_SELECTORS
    cards = []
    for card in soup.select(sel["card"]):
        title_el = card.select_one(sel["title"])
        if not title_el:
            continue
        href = title_el.get("href", "")
        detail_url = href if href.startswith("http") else sel["base_domain"] + href
        title = title_el.get_text(strip=True)

        company_divs = card.select(sel["company_divs"])
        # [0] = country/city, [1] = company name
        def _div_text(divs, idx):
            try:
                return divs[idx].select_one("p").get_text(strip=True)
            except (IndexError, AttributeError):
                return ""

        location = _div_text(company_divs, 0)
        company  = _div_text(company_divs, 1)

        cards.append({"title": title, "company": company, "location": location, "detail_url": detail_url})
    return cards


def scrape_relocateme(
    conn,
    categories: list[str],
    base_url_template: str,
) -> tuple[int, int]:
    added = skipped = 0
    seen_urls: set[str] = set()
    sel = RELOCATEME_SELECTORS

    for category in categories:
        url = base_url_template.format(category=category)
        resp = safe_get(url)
        if resp is None:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = _parse_relocateme_listing(soup)
        if not cards:
            log.warning("relocateme category=%r: 0 cards — selector may be broken (url=%s)", category, url)
        log.info("relocateme category=%r: %d cards", category, len(cards))

        for card in cards:
            detail_url = card["detail_url"]
            if detail_url in seen_urls:
                skipped += 1
                continue
            seen_urls.add(detail_url)

            # Skip detail fetch if already in DB
            if _url_in_db(conn, detail_url):
                skipped += 1
                continue

            detail_resp = safe_get(detail_url)
            if detail_resp is None:
                continue

            detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
            jd_el = None
            for cls_kw in sel["jd_classes"]:
                jd_el = detail_soup.find(class_=lambda c, k=cls_kw: c and k in c)
                if jd_el:
                    break
            jd_el = jd_el or detail_soup.find("main") or detail_soup.body
            raw_jd = jd_el.get_text(separator="\n", strip=True) if jd_el else ""

            record = {
                "id":          make_id(detail_url),
                "company":     card["company"],
                "title":       card["title"],
                "url":         detail_url,
                "source":      "relocateme",
                "source_tier": "auto",
                "location":    card["location"],
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(60),
                "status":      "un-scored",
            }
            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    return added, skipped


# ── Source 7: Ashby ───────────────────────────────────────────────────────────
#
# Public GraphQL API (no auth required, discovered via browser devtools).
# Endpoint: https://jobs.ashbyhq.com/api/non-user-graphql
#
# Step 1 – list all postings for a company:
#   jobBoardWithTeams(organizationHostedJobsPageName: $slug)
#   → { jobPostings { id title locationName workplaceType employmentType } }
#
# Step 2 – fetch full JD for each matched posting:
#   jobPosting(organizationHostedJobsPageName: $slug, jobPostingId: $id)
#   → { descriptionHtml }
#
# Canonical URL: https://jobs.ashbyhq.com/{slug}/{id}

ASHBY_API = "https://jobs.ashbyhq.com/api/non-user-graphql"
ASHBY_HEADERS = {
    **HEADERS,
    "Content-Type": "application/json",
    "Origin": "https://jobs.ashbyhq.com",
    "Referer": "https://jobs.ashbyhq.com/",
}

_ASHBY_BOARD_QUERY = """
query AshbyJobBoard($org: String!) {
  jobBoardWithTeams(organizationHostedJobsPageName: $org) {
    jobPostings {
      id
      title
      locationName
      workplaceType
      employmentType
    }
  }
}
"""

_ASHBY_DETAIL_QUERY = """
query AshbyJobPosting($org: String!, $id: String!) {
  jobPosting(organizationHostedJobsPageName: $org, jobPostingId: $id) {
    descriptionHtml
  }
}
"""


def _ashby_gql(query: str, variables: dict) -> dict | None:
    try:
        resp = requests.post(
            ASHBY_API,
            headers=ASHBY_HEADERS,
            json={"query": query, "variables": variables},
            timeout=12,
        )
        resp.raise_for_status()
        time.sleep(0.6)
        data = resp.json()
        if data.get("errors"):
            log.warning("ashby GQL errors: %s", data["errors"])
            return None
        return data.get("data")
    except Exception as exc:
        log.warning("ashby GQL failed: %s — %s", variables, exc)
        return None


def scrape_ashby(
    conn,
    companies: list[str],
    keywords: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]

    # Workplace types we consider remote-eligible
    remote_types = {"Remote", "Hybrid", None, ""}

    for slug in companies:
        data = _ashby_gql(_ASHBY_BOARD_QUERY, {"org": slug})
        if not data:
            continue

        board = data.get("jobBoardWithTeams")
        if not board:
            log.warning("ashby %s: null board (slug may be wrong)", slug)
            continue

        postings = board.get("jobPostings", [])
        log.info("ashby slug=%r: %d postings", slug, len(postings))

        for posting in postings:
            title: str = posting.get("title", "")
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            posting_id: str = posting.get("id", "")
            if not posting_id:
                continue

            location: str = posting.get("locationName") or ""
            workplace: str = posting.get("workplaceType") or ""

            job_url = f"https://jobs.ashbyhq.com/{slug}/{posting_id}"

            # Skip GQL detail fetch if already in DB
            if _url_in_db(conn, job_url):
                skipped += 1
                continue

            # Fetch full JD
            detail_data = _ashby_gql(_ASHBY_DETAIL_QUERY, {"org": slug, "id": posting_id})
            raw_jd = ""
            if detail_data and detail_data.get("jobPosting"):
                raw_jd = clean_html(detail_data["jobPosting"].get("descriptionHtml") or "")
            if not raw_jd:
                raw_jd = title

            record = {
                "id":          make_id(job_url),
                "company":     slug,
                "title":       title,
                "url":         job_url,
                "source":      "ashby",
                "source_tier": "auto",
                "location":    location or (workplace if workplace else ""),
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(60),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    log.info("ashby: total added=%d skipped=%d", added, skipped)
    return added, skipped


# ── Source 8: Workable ────────────────────────────────────────────────────────
#
# Public REST API (no auth required, confirmed from JS bundle analysis).
# Rate limit: aggressive (~30 req/min); scraper uses 2 s delay + exponential
# backoff (30 / 60 / 120 s) on 429.
#
# Listing : POST https://apply.workable.com/api/v3/accounts/{slug}/jobs
#             body: {"query":"","location":[],"department":[],"worktype":[],"remote":[]}
#             → {"total": N, "results": [{shortcode, title, department, location, ...}]}
#
# Detail  : GET  https://apply.workable.com/api/v3/accounts/{slug}/jobs/{shortcode}
#             → {shortcode, title, full_description, requirements, benefits, location, ...}
#
# Job URL : https://apply.workable.com/{slug}/j/{shortcode}
# 404     : company not on Workable or slug wrong — skip silently.

WORKABLE_BASE = "https://apply.workable.com"
WORKABLE_LISTING_BODY = {
    "query": "", "location": [], "department": [], "worktype": [], "remote": [],
}


def _workable_request(method: str, url: str, **kwargs) -> requests.Response | None:
    """Shared HTTP helper for Workable with 429 backoff (10 / 30 / 90 s, 3 retries)."""
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", 12)
    for attempt in range(3):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = 10 * (3 ** attempt)   # 10 → 30 → 90s
                log.warning("workable 429 %s — waiting %ds (attempt %d/3)", url, wait, attempt + 1)
                time.sleep(wait)
                continue
            return resp
        except Exception as exc:
            log.warning("workable request %s: %s", url, exc)
            return None
    log.warning("workable: exhausted retries on 429 for %s — skipping", url)
    return None


def _workable_post(slug: str) -> list[dict] | None:
    """Fetch all job listings for a company slug. Returns list or None on error."""
    url = f"{WORKABLE_BASE}/api/v3/accounts/{slug}/jobs"
    resp = _workable_request(
        "POST", url,
        headers={**HEADERS, "Content-Type": "application/json"},
        json=WORKABLE_LISTING_BODY,
    )
    if resp is None:
        return None
    if resp.status_code == 404:
        return None
    if not resp.ok:
        log.warning("workable %s: HTTP %d", slug, resp.status_code)
        return None
    time.sleep(2.0)
    try:
        return resp.json().get("results", [])
    except Exception:
        return None


def _workable_detail(slug: str, shortcode: str) -> str:
    """Fetch full JD for one job. Returns plain text or empty string."""
    url = f"{WORKABLE_BASE}/api/v3/accounts/{slug}/jobs/{shortcode}"
    resp = _workable_request("GET", url)
    if resp is None or not resp.ok:
        return ""
    time.sleep(2.0)
    try:
        data = resp.json()
        parts = [
            data.get("full_description") or "",
            data.get("requirements") or "",
            data.get("benefits") or "",
        ]
        return clean_html("\n".join(p for p in parts if p))
    except Exception:
        return ""


def scrape_workable(
    conn,
    companies: list[str],
    keywords: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]

    for slug in companies:
        jobs = _workable_post(slug)
        if jobs is None:
            log.warning("workable %s: 404 or fetch failed — skipping", slug)
            continue

        log.info("workable slug=%r: %d listings", slug, len(jobs))

        for job in jobs:
            title: str = job.get("title", "")
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            shortcode: str = job.get("shortcode", "")
            if not shortcode:
                continue

            loc: dict = job.get("location") or {}
            location = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))

            job_url = f"{WORKABLE_BASE}/{slug}/j/{shortcode}"

            # Skip detail fetch if already in DB
            if _url_in_db(conn, job_url):
                skipped += 1
                continue

            raw_jd = _workable_detail(slug, shortcode)
            if not raw_jd:
                raw_jd = title

            record = {
                "id":          make_id(job_url),
                "company":     job.get("company", {}).get("name", slug) if isinstance(job.get("company"), dict) else slug,
                "title":       title,
                "url":         job_url,
                "source":      "workable",
                "source_tier": "auto",
                "location":    location,
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(45),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    log.info("workable: total added=%d skipped=%d", added, skipped)
    return added, skipped


# ── Source 9: We Work Remotely ────────────────────────────────────────────────
#
# Public RSS feeds — no auth, no pagination, full JD in <description>.
# Title format: "Company Name: Job Title"
# Region field: typically "Anywhere in the World" (all remote)
#
# Feed URLs (confirmed working):
#   /remote-jobs.rss                              — all categories (~700 items)
#   /categories/remote-programming-jobs.rss       — programming (~25 items)
#   /categories/remote-devops-sysadmin-jobs.rss   — devops (~40 items)

import xml.etree.ElementTree as ET  # stdlib, no extra dep


def scrape_weworkremotely(
    conn,
    feed_urls: list[str],
    keywords: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]
    seen_urls: set[str] = set()

    for feed_url in feed_urls:
        resp = safe_get(feed_url)
        if resp is None:
            continue

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            log.warning("weworkremotely: XML parse error %s — %s", feed_url, exc)
            continue

        items = root.findall("channel/item")
        log.info("weworkremotely feed=%r: %d items", feed_url.split("/")[-1], len(items))

        for item in items:
            raw_title: str = item.findtext("title") or ""
            job_url: str = item.findtext("link") or item.findtext("guid") or ""

            if not job_url or job_url in seen_urls:
                continue
            seen_urls.add(job_url)

            # Title format: "Company: Role" — split on first colon
            if ":" in raw_title:
                company, title = raw_title.split(":", 1)
                company = company.strip()
                title = title.strip()
            else:
                company = ""
                title = raw_title.strip()

            # Keyword filter on title
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            region: str = item.findtext("region") or "Remote"
            desc_html: str = item.findtext("description") or ""
            raw_jd = clean_html(desc_html) if desc_html else title

            pub_date: str = item.findtext("pubDate") or ""

            record = {
                "id":          make_id(job_url),
                "company":     company,
                "title":       title,
                "url":         job_url,
                "source":      "weworkremotely",
                "source_tier": "auto",
                "location":    region,
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(30),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    return added, skipped


# ── Source 10: Jobicy ─────────────────────────────────────────────────────────

JOBICY_API = "https://jobicy.com/api/v2/remote-jobs"


def scrape_jobicy(
    conn,
    tags: list[str],
    count: int,
    exclude_geo: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    exclude_lower = [g.lower() for g in exclude_geo]

    for tag in tags:
        resp = safe_get(JOBICY_API, params={"count": count, "tag": tag})
        if resp is None:
            continue

        jobs = resp.json().get("jobs", [])
        log.info("jobicy tag=%r: %d 筆", tag, len(jobs))

        for job in jobs:
            # Exclude geo-restricted roles
            geo = job.get("jobGeo", "") or ""
            if any(ex in geo.lower() for ex in exclude_lower):
                continue

            job_url = job.get("url", "")
            if not job_url:
                continue

            raw_jd = clean_html(job.get("jobDescription", ""))

            record = {
                "id":          make_id(job_url),
                "company":     job.get("companyName", ""),
                "title":       job.get("jobTitle", ""),
                "url":         job_url,
                "source":      "jobicy",
                "source_tier": "auto",
                "location":    geo or "Remote",
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(60),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    return added, skipped


# ── Source 8: Greenhouse ──────────────────────────────────────────────────────
#
# Public Job Board API — no auth required.
# GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
# Returns all open jobs with HTML job description in `content` field.
# 404 means the company doesn't use Greenhouse — skip silently.


def scrape_greenhouse(
    conn,
    companies: list[str],
    keywords: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]

    for slug in companies:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        resp = safe_get(url, log_404=False, params={"content": "true"})
        if resp is None:
            # 404 = company not on Greenhouse or slug wrong — skip silently
            continue

        try:
            jobs = resp.json().get("jobs", [])
        except Exception:
            log.warning("greenhouse %s: invalid JSON response", slug)
            continue

        log.info("greenhouse slug=%r: %d jobs", slug, len(jobs))

        for job in jobs:
            title = job.get("title", "")
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            job_url = job.get("absolute_url", "")
            if not job_url:
                continue

            location = ""
            loc_obj = job.get("location")
            if loc_obj:
                location = loc_obj.get("name", "")

            raw_jd = clean_html(job.get("content", "")) or title

            record = {
                "id":          make_id(job_url),
                "company":     slug,
                "title":       title,
                "url":         job_url,
                "source":      "greenhouse",
                "source_tier": "auto",
                "location":    location,
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(90),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    return added, skipped


# ── Source N: Heise Jobs ─────────────────────────────────────────────────────
#
# German IT-focused job aggregator (76k+ listings).
# No auth, no API key — SSR Next.js page with jobs embedded in HTML.
#
# Search URL: GET https://jobs.heise.de/search?q={keyword}&loc={location}&page={N}
# Pagination:  page=N is CUMULATIVE — page=3 returns page1+page2+page3 jobs (≈56)
# Parse:       <li data-id="{jobId}"> cards with title, company, location, snippet
# Job URL:     https://jobs.heise.de/search?selected={jobId}  (canonical link)
#
# Snippet note: some cards embed 2000+ char JDs; most have 100-500 chars or none.
# The <p> inside each <li> is used as raw_jd_text (partial but sufficient for scoring).

_HEISE_SEARCH = "https://jobs.heise.de/search"
_HEISE_BADGE_RE = re.compile(r"^(Top|Premium|Anzeige)\s*", re.IGNORECASE)


def scrape_heise(
    conn,
    keywords: list[str],
    locations: list[str],
    max_pages: int = 3,
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]
    seen_ids: set[str] = set()

    for keyword in keywords:
        for loc in locations:
            # Cumulative pagination: page=max_pages returns all results at once
            resp = safe_get(
                _HEISE_SEARCH,
                params={"q": keyword, "loc": loc, "page": max_pages},
            )
            if resp is None:
                continue

            try:
                soup = BeautifulSoup(resp.text, "html.parser")
            except Exception as exc:
                log.warning("heise: BeautifulSoup error — %s", exc)
                continue

            cards = soup.select("li[data-id]")
            log.info("heise q=%r loc=%r: %d cards", keyword, loc, len(cards))

            for card in cards:
                job_id: str = card.get("data-id", "").strip()
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Title — strip badge prefix ("Top", "Premium", etc.)
                title_el = card.select_one("[data-testid*='title']")
                title = _HEISE_BADGE_RE.sub("", title_el.get_text(strip=True)) if title_el else ""
                if not title:
                    continue

                # Keyword filter on title
                if not any(kw in title.lower() for kw in kw_lower):
                    continue

                # Company — last <span> in first <section>
                logo_sec = card.select_one("section")
                spans = logo_sec.select("span") if logo_sec else []
                company = spans[-1].get_text(strip=True) if spans else ""

                # Location
                loc_el = card.select_one("[class*='loc'] span")
                job_location = loc_el.get_text(strip=True) if loc_el else loc

                # JD snippet — the <p> inside the card (may be 0–2000+ chars)
                snippet_el = card.select_one("p")
                raw_jd = snippet_el.get_text(separator="\n", strip=True) if snippet_el else ""
                if not raw_jd:
                    raw_jd = title

                job_url = f"https://jobs.heise.de/search?selected={job_id}"

                record = {
                    "id":          make_id(f"heise:{job_id}"),
                    "company":     company,
                    "title":       title,
                    "url":         job_url,
                    "source":      "heise",
                    "source_tier": "auto",
                    "location":    job_location,
                    "raw_jd_text": raw_jd,
                    "fetched_at":  utcnow(),
                    "expires_at":  expiry(30),
                    "status":      "un-scored",
                }

                _warn_empty_jd(record)
                if upsert_job(conn, record):
                    added += 1
                else:
                    skipped += 1

    log.info("heise: total added=%d skipped=%d", added, skipped)
    return added, skipped


# ── Source N: Personio ───────────────────────────────────────────────────────
#
# Public XML feed — no auth required.
# GET https://{slug}.jobs.personio.de/xml
#   → 200 XML  : valid company with all open positions + full JD in CDATA
#   → 301 to personio.de : slug doesn't exist or company no longer on Personio
#
# Job URL pattern: https://{slug}.jobs.personio.de/job/{id}
# XML schema  : <workzag-jobs><position><id>, <name>, <office>, <department>,
#                 <jobDescriptions><jobDescription><name>/<value>…
#               </position>…</workzag-jobs>
#
# Note: safe_get raises on redirect (HTTPError), so we detect 301 via
#       allow_redirects=False and a 3xx status check.


def scrape_personio(
    conn,
    companies: list[str],
    keywords: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]

    for slug in companies:
        url = f"https://{slug}.jobs.personio.de/xml"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=False)
        except Exception as exc:
            log.warning("personio %s: request error — %s", slug, exc)
            continue

        # 3xx → company not on Personio (301 → personio.de homepage)
        if resp.is_redirect or resp.status_code in (301, 302, 404):
            log.debug("personio %s: redirect/404 — skipping", slug)
            continue

        if not resp.ok:
            log.warning("personio %s: HTTP %d — skipping", slug, resp.status_code)
            continue

        time.sleep(1.0)

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            log.warning("personio %s: XML parse error — %s", slug, exc)
            continue

        positions = root.findall("position")
        log.info("personio slug=%r: %d positions", slug, len(positions))

        for pos in positions:
            title: str = (pos.findtext("name") or "").strip()
            if not title:
                continue

            # Keyword filter on title
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            pos_id: str = (pos.findtext("id") or "").strip()
            if not pos_id:
                continue

            job_url = f"https://{slug}.jobs.personio.de/job/{pos_id}"
            office: str = (pos.findtext("office") or "").strip()
            department: str = (pos.findtext("department") or "").strip()
            location = ", ".join(filter(None, [office, department])) or slug

            # Build JD from all <jobDescription> sections
            jd_parts: list[str] = []
            for jd_el in pos.findall("jobDescriptions/jobDescription"):
                section_name = jd_el.findtext("name") or ""
                value_html = jd_el.findtext("value") or ""
                section_text = clean_html(value_html)
                if section_text:
                    jd_parts.append(f"{section_name}\n{section_text}" if section_name else section_text)
            raw_jd = "\n\n".join(jd_parts) or title

            created_at: str = pos.findtext("createdAt") or utcnow()

            record = {
                "id":          make_id(job_url),
                "company":     slug,
                "title":       title,
                "url":         job_url,
                "source":      "personio",
                "source_tier": "auto",
                "location":    location,
                "raw_jd_text": raw_jd,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(60),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    log.info("personio: total added=%d skipped=%d", added, skipped)
    return added, skipped


# ── Source 9: Lever ───────────────────────────────────────────────────────────
#
# Public Postings API — no auth required.
# GET https://api.lever.co/v0/postings/{slug}?mode=json
# Returns list of open postings with plain-text description.
# 404 means the company doesn't use Lever — skip silently.

# ── Welcome to the Jungle (WTTJ) ──────────────────────────────────────────────
# Algolia-backed public search API (credentials embedded in HTML, no auth wall).
# Filters: language:en AND (remote:fulltime OR offices.country_code:DE)
# Gives EU startup remote jobs + German office roles in English.
# API key is scoped to search-only reads; safe to embed here.

_WTTJ_APP_ID  = "CSEKHVMS53"
_WTTJ_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
_WTTJ_INDEX   = "wttj_jobs_production_en"
_WTTJ_ENDPOINT = (
    f"https://{_WTTJ_APP_ID}-dsn.algolia.net"
    f"/1/indexes/{_WTTJ_INDEX}/query"
)
_WTTJ_HEADERS = {
    "X-Algolia-Application-Id": _WTTJ_APP_ID,
    "X-Algolia-API-Key":        _WTTJ_API_KEY,
    "Referer":                  "https://www.welcometothejungle.com/en/jobs",
    "Content-Type":             "application/json",
    "User-Agent":               HEADERS["User-Agent"],
}
_WTTJ_FILTERS = "language:en AND (remote:fulltime OR offices.country_code:DE)"


def scrape_wttj(
    conn,
    keywords: list[str],
    hits_per_page: int = 100,
) -> tuple[int, int]:
    """Scrape Welcome to the Jungle via its Algolia search API."""
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]
    seen_refs: set[str] = set()

    for keyword in keywords:
        page = 0
        while True:
            page_added = 0
            payload = {
                "query":        keyword,
                "hitsPerPage":  hits_per_page,
                "page":         page,
                "filters":      _WTTJ_FILTERS,
            }
            try:
                resp = requests.post(
                    _WTTJ_ENDPOINT,
                    headers=_WTTJ_HEADERS,
                    json=payload,
                    timeout=15,
                )
            except Exception as exc:
                log.warning("wttj q=%r page=%d: request error — %s", keyword, page, exc)
                break

            if not resp.ok:
                log.warning("wttj q=%r page=%d: HTTP %d", keyword, page, resp.status_code)
                break

            try:
                data = resp.json()
            except Exception:
                log.warning("wttj q=%r page=%d: invalid JSON", keyword, page)
                break

            hits      = data.get("hits", [])
            nb_pages  = data.get("nbPages", 1)
            log.info("wttj q=%r page=%d/%d: %d hits", keyword, page + 1, nb_pages, len(hits))

            for h in hits:
                ref = h.get("reference", "")
                if not ref or ref in seen_refs:
                    continue

                title = (h.get("name") or "").strip()
                if not title:
                    continue
                # keyword relevance check — Algolia already does this but double-check
                if not any(kw in title.lower() for kw in kw_lower):
                    # also check summary / key_missions text
                    summary_text = (h.get("summary") or "").lower()
                    missions_text = " ".join(h.get("key_missions") or []).lower()
                    if not any(kw in summary_text or kw in missions_text for kw in kw_lower):
                        continue

                seen_refs.add(ref)

                org       = h.get("organization") or {}
                org_ref   = org.get("reference", "")
                slug      = h.get("slug", "")
                company   = org.get("name", "")

                if org_ref and slug:
                    job_url = (
                        f"https://www.welcometothejungle.com"
                        f"/en/companies/{org_ref}/jobs/{slug}"
                    )
                else:
                    job_url = f"https://www.welcometothejungle.com/en/jobs/{ref}"

                # Location: offices list → first DE office or first city
                offices = h.get("offices") or []
                de_offices = [o for o in offices if o.get("country_code") == "DE"]
                loc_office  = de_offices[0] if de_offices else (offices[0] if offices else {})
                city        = loc_office.get("city", "")
                country     = loc_office.get("country_code", "")
                remote_type = h.get("remote", "")
                if remote_type == "fulltime":
                    location = f"Remote{(' / ' + city) if city else ''}"
                else:
                    location = ", ".join(filter(None, [city, country])) or "DE"

                # JD: summary + key_missions bullets + profile requirements
                summary       = (h.get("summary") or "").strip()
                key_missions  = h.get("key_missions") or []
                profile       = (h.get("profile") or "").strip()

                parts = []
                if summary:
                    parts.append(summary)
                if isinstance(key_missions, list):
                    parts.extend(m for m in key_missions if m)
                elif key_missions:
                    parts.append(str(key_missions))
                if profile:
                    parts.append(profile)

                raw_jd = "\n\n".join(parts) or title

                record = {
                    "id":          make_id(job_url),
                    "company":     company,
                    "title":       title,
                    "url":         job_url,
                    "source":      "wttj",
                    "source_tier": "auto",
                    "location":    location,
                    "raw_jd_text": raw_jd,
                    "fetched_at":  utcnow(),
                    "expires_at":  expiry(30),
                    "status":      "un-scored",
                }

                _warn_empty_jd(record)
                if upsert_job(conn, record):
                    added += 1
                    page_added += 1
                else:
                    skipped += 1

            # Early exit: results are newest-first; 0 new on this page → nothing newer ahead
            if page_added == 0:
                break
            if page + 1 >= nb_pages:
                break
            page += 1
            time.sleep(0.3)  # Algolia is generous but be polite

    log.info("wttj: total added=%d skipped=%d", added, skipped)
    return added, skipped


def scrape_lever(
    conn,
    companies: list[str],
    keywords: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    kw_lower = [k.lower() for k in keywords]

    for slug in companies:
        url = f"https://api.lever.co/v0/postings/{slug}"
        resp = safe_get(url, log_404=False, params={"mode": "json"})
        if resp is None:
            # 404 = company not on Lever or slug wrong — skip silently
            continue

        try:
            data = resp.json()
            if not isinstance(data, list):
                log.warning("lever %s: unexpected response type %s", slug, type(data))
                continue
        except Exception:
            log.warning("lever %s: invalid JSON response", slug)
            continue

        log.info("lever slug=%r: %d postings", slug, len(data))

        for posting in data:
            title = posting.get("text", "")
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            job_url = posting.get("hostedUrl", "")
            if not job_url:
                job_url = f"https://jobs.lever.co/{slug}/{posting.get('id', '')}"

            categories = posting.get("categories", {})
            location = categories.get("location", "") or categories.get("allLocations", [""])[0]

            # Lever provides `descriptionPlain` (plain text) and `description` (HTML)
            plain = posting.get("descriptionPlain", "")
            html_desc = posting.get("description", "")
            raw_jd = plain if plain else clean_html(html_desc)

            # Append list items (additional requirements sections)
            for section in posting.get("lists", []):
                section_text = clean_html(section.get("content", ""))
                if section_text:
                    raw_jd += f"\n{section.get('text', '')}\n{section_text}"

            company_name = posting.get("company", slug)

            record = {
                "id":          make_id(job_url),
                "company":     company_name,
                "title":       title,
                "url":         job_url,
                "source":      "lever",
                "source_tier": "auto",
                "location":    location,
                "raw_jd_text": raw_jd or title,
                "fetched_at":  utcnow(),
                "expires_at":  expiry(90),
                "status":      "un-scored",
            }

            _warn_empty_jd(record)
            if upsert_job(conn, record):
                added += 1
            else:
                skipped += 1

    return added, skipped


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_start = datetime.now(timezone.utc)
    log.info("=== Phase 1 開始 %s ===", run_start.strftime("%Y-%m-%dT%H:%M:%S"))

    conn = init_db(DB_PATH)

    with open("config/search_targets.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    results: dict[str, tuple[int, int]] = {}

    # ── Arbeitnow ──
    log.info("scraping arbeitnow …")
    an = config["arbeitnow"]
    results["arbeitnow"] = scrape_arbeitnow(
        conn,
        keywords=an["keywords"],
        locations=an["locations"],
        remote_filter=an.get("remote_filter", True),
        max_pages=an.get("max_pages", 5),
    )

    # ── WeAreDevelopers ──
    log.info("scraping wearedevelopers …")
    wad = config.get("wearedevelopers", {})
    if wad.get("keywords"):
        results["wearedevelopers"] = scrape_wearedevelopers(
            conn,
            keywords=wad["keywords"],
            max_pages=wad.get("max_pages", 3),
            per_page=wad.get("per_page", 100),
        )
    else:
        log.info("wearedevelopers: not configured — skipping")
        results["wearedevelopers"] = (0, 0)

    # ── EnglishJobs ──
    log.info("scraping englishjobs …")
    ej = config["englishjobs"]
    results["englishjobs"] = scrape_englishjobs(
        conn,
        base_url=ej["base_url"],
        keywords=ej["keywords"],
        max_pages=ej.get("max_pages", 3),
    )

    # ── GermanTechJobs ──
    log.info("scraping germantechjobs …")
    gtj = config.get("germantechjobs", {})
    results["germantechjobs"] = scrape_germantechjobs(
        conn,
        keywords=gtj.get("keywords", []),
        target_cities=set(gtj.get("target_cities", [])) or None,
        include_remote=gtj.get("include_remote", True),
        max_detail_fetches=gtj.get("max_detail_fetches", 50),
    )

    # ── Bundesagentur ──
    log.info("scraping bundesagentur …")
    ba = config["bundesagentur"]
    results["bundesagentur"] = scrape_bundesagentur(
        conn,
        keywords=ba["keywords"],
        location=ba["location"],
        radius_km=ba.get("radius_km", 50),
        include_remote=ba.get("include_remote", True),
        size=ba.get("size", 100),
    )

    # ── Remotive ──
    log.info("scraping remotive …")
    rm = config["remotive"]
    results["remotive"] = scrape_remotive(
        conn,
        categories=rm["categories"],
        keywords=rm["keywords"],
        limit=rm.get("limit", 100),
    )

    # ── Relocate.me ──
    log.info("scraping relocateme …")
    rm2 = config["relocateme"]
    results["relocateme"] = scrape_relocateme(
        conn,
        categories=rm2["categories"],
        base_url_template=rm2["base_url"],
    )

    # ── Jobicy ──
    log.info("scraping jobicy …")
    jc = config["jobicy"]
    results["jobicy"] = scrape_jobicy(
        conn,
        tags=jc["tags"],
        count=jc.get("count", 50),
        exclude_geo=jc.get("exclude_geo", []),
    )

    # ── Ashby ──
    log.info("scraping ashby …")
    ab = config.get("ashby", {})
    if ab.get("companies"):
        results["ashby"] = scrape_ashby(
            conn,
            companies=ab["companies"],
            keywords=ab.get("keywords", []),
        )
    else:
        log.info("ashby: no companies configured — skipping")
        results["ashby"] = (0, 0)

    # ── Workable ──
    log.info("scraping workable …")
    wb = config.get("workable", {})
    if wb.get("companies"):
        results["workable"] = scrape_workable(
            conn,
            companies=wb["companies"],
            keywords=wb.get("keywords", []),
        )
    else:
        log.info("workable: no companies configured — skipping")
        results["workable"] = (0, 0)

    # ── We Work Remotely ──
    log.info("scraping weworkremotely …")
    wwr = config.get("weworkremotely", {})
    if wwr.get("feeds"):
        results["weworkremotely"] = scrape_weworkremotely(
            conn,
            feed_urls=wwr["feeds"],
            keywords=wwr.get("keywords", []),
        )
    else:
        log.info("weworkremotely: not configured — skipping")
        results["weworkremotely"] = (0, 0)

    # ── Greenhouse ──
    log.info("scraping greenhouse …")
    gh = config.get("greenhouse", {})
    if gh.get("companies"):
        results["greenhouse"] = scrape_greenhouse(
            conn,
            companies=gh["companies"],
            keywords=gh.get("keywords", []),
        )
    else:
        log.info("greenhouse: no companies configured — skipping")
        results["greenhouse"] = (0, 0)

    # ── Heise Jobs ──
    log.info("scraping heise …")
    hj = config.get("heise", {})
    if hj.get("keywords"):
        results["heise"] = scrape_heise(
            conn,
            keywords=hj["keywords"],
            locations=hj.get("locations", [""]),
            max_pages=hj.get("max_pages", 3),
        )
    else:
        log.info("heise: not configured — skipping")
        results["heise"] = (0, 0)

    # ── Personio ──
    log.info("scraping personio …")
    pn = config.get("personio", {})
    if pn.get("companies"):
        results["personio"] = scrape_personio(
            conn,
            companies=pn["companies"],
            keywords=pn.get("keywords", []),
        )
    else:
        log.info("personio: no companies configured — skipping")
        results["personio"] = (0, 0)

    # ── Welcome to the Jungle (WTTJ) ──
    log.info("scraping wttj …")
    wttj = config.get("wttj", {})
    if wttj.get("keywords"):
        results["wttj"] = scrape_wttj(
            conn,
            keywords=wttj["keywords"],
            hits_per_page=wttj.get("hits_per_page", 100),
        )
    else:
        log.info("wttj: not configured — skipping")
        results["wttj"] = (0, 0)

    # ── Lever ──
    log.info("scraping lever …")
    lv = config.get("lever", {})
    if lv.get("companies"):
        results["lever"] = scrape_lever(
            conn,
            companies=lv["companies"],
            keywords=lv.get("keywords", []),
        )
    else:
        log.info("lever: no companies configured — skipping")
        results["lever"] = (0, 0)

    for source, (added, skipped) in results.items():
        log.info("  %-16s 新增 %3d 筆，略過 %3d 筆（重複）", source, added, skipped)

    total_added = sum(v[0] for v in results.values())
    total_skipped = sum(v[1] for v in results.values())
    elapsed = (datetime.now(timezone.utc) - run_start).seconds
    log.info("=== Phase 1 完成 | 新增 %d 筆，略過 %d 筆 | 耗時 %ds | DB: %s ===",
             total_added, total_skipped, elapsed, DB_PATH)

    conn.close()
