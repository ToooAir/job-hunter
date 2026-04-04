"""
utils/company_researcher.py — Scrape company about page + LLM summary.

Called from the dashboard when the user clicks "🔍 公司研究".
No additional dependencies beyond what the project already uses.
"""

import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from utils.db import init_db, fetch_job_by_id, set_company_research
from utils.llm import make_client, chat_model

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-hunter-bot/1.0)"}
_TIMEOUT = 8

# Paths to try on the company domain, in order
_ABOUT_PATHS = ["/about", "/about-us", "/company", "/en/about", "/company/about"]

# Domains that are job boards, not company sites — skip scraping these
_BOARD_DOMAINS = {
    "greenhouse.io", "lever.co", "boards.greenhouse.io",
    "arbeitnow.com", "englishjobs.de", "remotive.com",
    "relocate.me", "jobicy.com", "bundesagentur.de",
    "arbeitsagentur.de", "linkedin.com", "stepstone.de",
    "xing.com", "indeed.com",
}


def _extract_domain(url: str) -> str | None:
    """Return netloc without www., or None if it's a known job-board domain."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().lstrip("www.")
        for board in _BOARD_DOMAINS:
            if host == board or host.endswith("." + board):
                return None
        return host
    except Exception:
        return None


def _fetch_text(url: str, max_chars: int = 6000) -> str:
    """GET url, strip HTML tags, return plain text up to max_chars."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Collapse whitespace
        text = re.sub(r"\s{2,}", " ", text)
        return text[:max_chars]
    except Exception as exc:
        log.debug("Failed to fetch %s: %s", url, exc)
        return ""


def _scrape_company_about(job_url: str, company: str) -> str:
    """Try to scrape the company's about page. Returns text or empty string."""
    domain = _extract_domain(job_url)
    if not domain:
        log.debug("Job URL is a job board, skipping scrape for %s", company)
        return ""

    for path in _ABOUT_PATHS:
        url = f"https://{domain}{path}"
        text = _fetch_text(url)
        if len(text) > 200:
            log.info("Scraped %s (%d chars) for %s", url, len(text), company)
            return text

    # Fall back to homepage
    text = _fetch_text(f"https://{domain}")
    if len(text) > 200:
        log.info("Scraped homepage %s (%d chars) for %s", domain, len(text), company)
    return text


_RESEARCH_PROMPT = """\
You are a job-seeker's research assistant. Based on the information below, produce a concise \
company profile in Traditional Chinese (繁體中文). Use Markdown.

### 公司概況
（成立年份、總部、規模 / 員工數、產品或服務 — 若資訊不明確請標注「不明」）

### 技術棧
（從 JD 或公開資訊推斷；未知的請略過）

### 文化與工作環境
（工作語言、遠端政策、已知文化特色）

### 簽證 / 國際化友善度
（是否有國際團隊、是否提供 relocation、對 Chancenkarte 的可能態度）

### 值得關注
（值得在面試中提及的亮點，或需要留意的風險，各 1–3 點）

Keep each section to 2–4 bullet points. Do not invent facts — mark uncertain items with「（推測）」.

---

Company name: {company}
Job URL: {job_url}

Job description (excerpt):
{jd_text}

Company about page (scraped, may be incomplete):
{scraped_text}
"""


def research_company(
    job_id: str,
    db_path: str,
    qdrant_path: str | None = None,  # unused, kept for call-site symmetry
) -> str | None:
    """Generate and persist a company research brief. Returns brief text or None."""
    conn = init_db(db_path)
    job = fetch_job_by_id(conn, job_id)
    if job is None:
        conn.close()
        log.warning("research_company: job %s not found", job_id)
        return None

    company  = job["company"]
    job_url  = job["url"]
    jd_text  = (job.get("raw_jd_text") or "")[:4000]

    scraped_text = _scrape_company_about(job_url, company)

    prompt = _RESEARCH_PROMPT.format(
        company=company,
        job_url=job_url,
        jd_text=jd_text,
        scraped_text=scraped_text or "（無法取得公開頁面資訊）",
    )

    client = make_client()
    try:
        resp = client.chat.completions.create(
            model=chat_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        brief = resp.choices[0].message.content.strip()
        set_company_research(conn, job_id, brief)
        conn.close()
        log.info("company research generated for %s (%s)", company, job_id)
        return brief
    except Exception as exc:
        log.error("Failed to research company %s: %s", company, exc)
        conn.close()
        return None
