"""
Phase 1 — Job Ingestor
Scrapes arbeitnow, englishjobs.de, and germantechjobs.de,
then upserts raw job listings into the SQLite database.
"""

import hashlib
import logging
import os
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
    # 卡片 id 屬性即為 job hash，用於組詳情 URL
    "detail_url_tpl": "https://englishjobs.de/jobs/{job_id}",
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
        detail_url = sel["detail_url_tpl"].format(job_id=job_id)

        title_el = card.select_one(sel["title"])
        title = title_el.get_text(strip=True) if title_el else ""

        lis = card.select("ul li")
        company  = lis[0].get_text(strip=True) if len(lis) > 0 else ""
        location = lis[1].get_text(strip=True) if len(lis) > 1 else ""

        cards.append({"title": title, "company": company, "location": location, "detail_url": detail_url})
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
                detail_url = card["detail_url"]
                if detail_url in seen_urls:
                    skipped += 1
                    continue
                seen_urls.add(detail_url)

                detail_resp = safe_get(detail_url)
                if detail_resp is None:
                    continue

                detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                # EnglishJobs detail page embeds the same card with a snippet;
                # grab the full card text as the JD.
                jd_el = detail_soup.select_one(ENGLISHJOBS_SELECTORS["card"]) or detail_soup.body
                raw_jd = jd_el.get_text(separator="\n", strip=True) if jd_el else ""

                record = {
                    "id":          make_id(detail_url),
                    "company":     card["company"],
                    "title":       card["title"],
                    "url":         detail_url,
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


def _parse_germantechjobs_listing(soup: BeautifulSoup) -> list[dict]:
    """Extract job cards from a germantechjobs.de listing page."""
    sel = GERMANTECHJOBS_SELECTORS
    href_kw = sel["detail_href"]
    cards = []

    items = soup.select(sel["card_primary"])
    if not items:
        items = [
            a.find_parent("li") or a.find_parent("div")
            for a in soup.find_all("a", href=lambda h: h and href_kw in h)
            if a.find_parent("li") or a.find_parent("div")
        ]
        seen = set()
        items = [x for x in items if x and id(x) not in seen and not seen.add(id(x))]

    for item in items:
        a_tag = item.find("a", href=lambda h: h and href_kw in h) if item else None
        if not a_tag:
            continue
        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else urljoin(sel["base_domain"], href)

        title_el = item.find(["h2", "h3", "h4"]) or a_tag
        title = title_el.get_text(strip=True)

        company_el = item.find(class_=lambda c: c and "company" in c)
        company = company_el.get_text(strip=True) if company_el else ""

        location_el = item.find(class_=lambda c: c and "location" in c)
        location = location_el.get_text(strip=True) if location_el else ""

        cards.append(
            {"title": title, "company": company, "location": location, "detail_url": detail_url}
        )
    return cards


def scrape_germantechjobs(
    conn,
    base_url_template: str,
    keywords: list[str],
    locations: list[str],
) -> tuple[int, int]:
    added = skipped = 0
    seen_urls: set[str] = set()

    for keyword in keywords:
        for location in locations:
            url = base_url_template.format(keyword=keyword, location=location)
            resp = safe_get(url)
            if resp is None:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = _parse_germantechjobs_listing(soup)

            for card in cards:
                detail_url = card["detail_url"]
                if detail_url in seen_urls:
                    skipped += 1
                    continue
                seen_urls.add(detail_url)

                detail_resp = safe_get(detail_url)
                if detail_resp is None:
                    continue

                detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                jd_el = None
                for kw in GERMANTECHJOBS_SELECTORS["jd_classes"]:
                    jd_el = detail_soup.find(class_=lambda c, k=kw: c and k in c)
                    if jd_el:
                        break
                jd_el = jd_el or detail_soup.find("article") or detail_soup.find("main") or detail_soup.body
                raw_jd = jd_el.get_text(separator="\n", strip=True) if jd_el else ""

                record = {
                    "id":          make_id(detail_url),
                    "company":     card["company"],
                    "title":       card["title"],
                    "url":         detail_url,
                    "source":      "germantechjobs",
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
                "germantechjobs kw=%r loc=%r: added=%d skipped=%d",
                keyword, location, added, skipped,
            )

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


# ── Source 5: Remotive ─────────────────────────────────────────────────────────

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


# ── Source 7: Jobicy ──────────────────────────────────────────────────────────

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


# ── Source 9: Lever ───────────────────────────────────────────────────────────
#
# Public Postings API — no auth required.
# GET https://api.lever.co/v0/postings/{slug}?mode=json
# Returns list of open postings with plain-text description.
# 404 means the company doesn't use Lever — skip silently.


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
    an = config["arbeitnow"]
    results["arbeitnow"] = scrape_arbeitnow(
        conn,
        keywords=an["keywords"],
        locations=an["locations"],
        remote_filter=an.get("remote_filter", True),
        max_pages=an.get("max_pages", 5),
    )

    # ── EnglishJobs ──
    ej = config["englishjobs"]
    results["englishjobs"] = scrape_englishjobs(
        conn,
        base_url=ej["base_url"],
        keywords=ej["keywords"],
        max_pages=ej.get("max_pages", 3),
    )

    # ── GermanTechJobs（JS 渲染 SPA，暫停；需 Playwright 才能爬取）──
    log.warning("germantechjobs: JS-rendered SPA — skipped (needs Playwright)")
    results["germantechjobs"] = (0, 0)

    # ── Bundesagentur ──
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
    rm = config["remotive"]
    results["remotive"] = scrape_remotive(
        conn,
        categories=rm["categories"],
        keywords=rm["keywords"],
        limit=rm.get("limit", 100),
    )

    # ── Relocate.me ──
    rm2 = config["relocateme"]
    results["relocateme"] = scrape_relocateme(
        conn,
        categories=rm2["categories"],
        base_url_template=rm2["base_url"],
    )

    # ── Jobicy ──
    jc = config["jobicy"]
    results["jobicy"] = scrape_jobicy(
        conn,
        tags=jc["tags"],
        count=jc.get("count", 50),
        exclude_geo=jc.get("exclude_geo", []),
    )

    # ── Greenhouse ──
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

    # ── Lever ──
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
