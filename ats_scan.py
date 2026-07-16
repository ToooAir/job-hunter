#!/usr/bin/env python3
"""ats_scan.py — resolve job URLs to the ATS that hosts the apply form.

Scans the queue-eligible pool (Germany + Remote — EU, grade A or B>=65, status='scored'),
follows each job URL plus one level of apply links, and detects which ATS
the application actually lives on. WTTJ jobs are resolved via the public
WTTJ API, which also yields liveness (status/archived_at) and apply_url.

Re-runnable: this is the precursor of the pre-queue liveness gate (Step 2).

Output: summary table on stdout + per-job results in data/ats_scan.csv

Usage:
    python ats_scan.py                        # full scan
    python ats_scan.py --limit 10             # quick test
    python ats_scan.py --write-db             # scan and store ats/apply_url/ats_checked_at
    python ats_scan.py --from-csv data/ats_scan.csv --write-db
        # no network: import previous results (merging data/indeed_resolve.json
        # refinements when present); ats_checked_at = CSV file mtime
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from html import unescape as html_unescape
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from utils.apply_queue import MIN_B_SCORE, REMOTE_ELIGIBLE_LOCATIONS  # noqa: E402
from utils.db import init_db, set_job_ats  # noqa: E402
from utils.gone_text import redirect_off_posting, soft_gone  # noqa: E402

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "jobs.db"
OUT_CSV = ROOT / "data" / "ats_scan.csv"
INDEED_RESOLVE_JSON = ROOT / "data" / "indeed_resolve.json"

# indeed_resolve.json verdicts (CDP browser pass) → final ats value.
# company-site means the apply form lives on the employer's own website,
# i.e. the generic-form bucket → same handling as "unknown".
INDEED_VERDICT_TO_ATS = {
    "indeed-native": "indeed",
    "company-site": "unknown",
    "greenhouse": "greenhouse",
    "lever": "lever",
    "gone": "gone",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en,de;q=0.8",
}
TIMEOUT = 15
MAX_WORKERS = 4
MAX_APPLY_LINKS = 3  # depth-2 fetches per job

# Substring → ATS name. Order matters: first match wins.
ATS_PATTERNS = {
    "greenhouse": ["greenhouse.io"],
    "lever": ["jobs.lever.co", "jobs.eu.lever.co"],
    "ashby": ["ashbyhq.com"],
    "personio": ["jobs.personio.de", "jobs.personio.com", ".jobs.personio"],
    "workable": ["workable.com"],
    "join": ["join.com"],
    "smartrecruiters": ["smartrecruiters.com"],
    "workday": ["myworkdayjobs.com"],
    "successfactors": ["successfactors.", "jobs.sap.com"],
    "softgarden": ["softgarden.io", "softgarden.de"],
    "recruitee": ["recruitee.com"],
    "teamtailor": ["teamtailor.com"],
    "onlyfy": ["onlyfy.jobs", "prescreen.io"],
    "dvinci": ["dvinci.de", "dvinci-hr.com"],
    "rexx": ["rexx-systems.com", "rexx-recruitment.com"],
    "concludis": ["concludis.de"],
    "bite": ["b-ite.de", "b-ite.com"],
    "coveto": ["coveto.de"],
    "heyrecruit": ["heyrecruit.de"],
    "kenjo": ["kenjo.io"],
    "bamboohr": ["bamboohr.com"],
    "icims": ["icims.com"],
    "hibob": ["hibob.com"],
    # board-hosted apply flows (bot-hostile / manual only) — keep these LAST
    # so a real ATS link on the same page wins
    "indeed": ["indeed.com/viewjob", "indeed.com/rc/clk", "indeed.com/applystart"],
    "stepstone": ["stepstone.de/stellenangebote", "stepstone.de/cmp"],
    "linkedin": ["linkedin.com/jobs/view"],
}

GERMANY_LIKE = [
    "German", "Deutschland", "Hamburg", "Berlin", "Munich", "München",
    "Köln", "Cologne", "Frankfurt", "Stuttgart", "Düsseldorf", "Leipzig",
    "Bremen", "Hannover",
]

APPLY_RE = re.compile(r"apply|bewerb", re.I)
SKIP_HREF_RE = re.compile(r"^(#|javascript:|tel:)", re.I)

# WTTJ job pages are JS-rendered, but the public API serves everything:
# apply_url (direct ATS link), status/archived_at (liveness), and for
# native applications even the form schema (application_fields).
WTTJ_RE = re.compile(r"welcometothejungle\.com/[^/]+/companies/([^/]+)/jobs/([^/?#]+)")
WTTJ_API = "https://api.welcometothejungle.com/api/v1/organizations/{org}/jobs/{slug}"


def fetch_jobs(limit=None):
    """Queue-eligible pool: Germany (+ Remote — EU), grade A or B >= MIN_B_SCORE.
    Must mirror utils.apply_queue.fetch_candidates so scan pool == queue pool."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    loc_terms = [f"location LIKE '%{kw}%'" for kw in GERMANY_LIKE]
    loc_terms += [f"location = '{loc}'" for loc in REMOTE_ELIGIBLE_LOCATIONS]
    loc_clause = " OR ".join(loc_terms)
    sql = (
        "SELECT id, source, company, title, url, fit_grade, match_score FROM jobs "
        "WHERE status='scored' "
        f"AND (fit_grade='A' OR (fit_grade='B' AND match_score >= {MIN_B_SCORE})) "
        f"AND ({loc_clause}) "
        "ORDER BY match_score DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def classify_url(url):
    low = url.lower()
    for ats, pats in ATS_PATTERNS.items():
        for pat in pats:
            if pat in low:
                return ats
    return None


def scan_text_for_ats(text):
    """Find an ATS URL anywhere in raw HTML (covers JSON blobs like apply_url)."""
    text = text.replace("\\/", "/")  # Nuxt/JSON payloads escape slashes
    # HTML-entity-encoded JSON (&quot;) hides the quote that ends a URL, so the
    # match swallowed the config blob after it (idealo/recruitee, draft #100).
    text = html_unescape(text)
    low = text.lower()
    for ats, pats in ATS_PATTERNS.items():
        for pat in pats:
            idx = low.find(pat)
            if idx == -1:
                continue
            m = re.search(
                r"https?://[^\s\"'<>\\]*" + re.escape(pat) + r"[^\s\"'<>\\]*",
                text[max(0, idx - 200): idx + 300],
                re.I,
            )
            return ats, (m.group(0) if m else pat)
    return None, None


def extract_apply_links(soup, base_url):
    """Anchor hrefs whose href or text smells like an apply button."""
    links, seen = [], set()
    mailto = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if SKIP_HREF_RE.match(href):
            continue
        if href.lower().startswith("mailto:"):
            if mailto is None and APPLY_RE.search(href + a.get_text(" ")):
                mailto = href
            continue
        if APPLY_RE.search(href) or APPLY_RE.search(a.get_text(" ")):
            absolute = urljoin(base_url, href)
            if absolute not in seen:
                seen.add(absolute)
                links.append(absolute)
    return links[:MAX_APPLY_LINKS], mailto


def resolve_wttj(url, result):
    """Resolve a WTTJ job via the public API. Returns True if handled."""
    m = WTTJ_RE.search(url)
    if not m:
        return False
    api_url = WTTJ_API.format(org=m.group(1), slug=m.group(2))
    try:
        r = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        result.update(ats="fetch-error", evidence=str(e)[:200])
        return True
    if r.status_code in (404, 410):
        result.update(ats="gone", evidence=f"WTTJ API HTTP {r.status_code}")
        return True
    if r.status_code != 200:
        result.update(ats="fetch-error", evidence=f"WTTJ API HTTP {r.status_code}")
        return True
    job = (r.json() or {}).get("job") or {}
    if job.get("archived_at") or job.get("status") not in (None, "published"):
        result.update(
            ats="gone",
            evidence=f"wttj status={job.get('status')} archived_at={job.get('archived_at')}",
        )
        return True
    apply_url = job.get("apply_url") or ""
    ats = classify_url(apply_url) if apply_url else None
    if ats:
        result.update(ats=ats, evidence=apply_url)
    elif apply_url:
        result.update(ats="unknown-external", evidence=apply_url)
    else:
        n_fields = len(job.get("application_fields") or [])
        result.update(ats="wttj-native", evidence=f"native form, {n_fields} schema fields")
    return True


def resolve_one(job):
    url = job["url"]
    result = {
        "job_id": job["id"], "source": job["source"], "company": job["company"],
        "title": job["title"], "fit_grade": job["fit_grade"],
        "match_score": job["match_score"],
        "url": url, "ats": "unknown", "evidence": "",
    }
    if resolve_wttj(url, result):
        return result
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        result.update(ats="fetch-error", evidence=str(e)[:200])
        return result
    if r.status_code in (404, 410):
        result.update(ats="gone", evidence=f"HTTP {r.status_code}")
        return result
    # 200 with "position filled"-class wording = gone (soft-gone). Checked
    # before ATS classification: a closed Greenhouse page still links the ATS.
    gone_phrase = soft_gone(r.text)
    if gone_phrase:
        result.update(ats="gone", evidence=f"soft-gone: {gone_phrase[:80]}")
        return result

    # 1. HTTP redirect already landed on a known ATS
    ats = classify_url(r.url)
    if ats:
        result.update(ats=ats, evidence=r.url)
        return result

    # Same-board redirect that dropped the posting's slug = the listing was
    # taken down (germantechjobs bounces dead jobs to /jobs/<category>/all).
    # After the known-ATS check — a recognizable ATS landing always wins —
    # and before the page-content scans, which would otherwise run on some
    # other listing's HTML.
    if redirect_off_posting(url, r.url):
        result.update(ats="gone", evidence=f"redirected off the posting: {r.url[:120]}")
        return result

    # 2. ATS URL embedded anywhere in the page (hrefs, JSON apply_url, ...)
    ats, evidence = scan_text_for_ats(r.text)
    if ats:
        result.update(ats=ats, evidence=evidence)
        return result

    # 3. Follow apply-looking links one level deeper
    soup = BeautifulSoup(r.text, "html.parser")
    apply_links, mailto = extract_apply_links(soup, r.url)
    for link in apply_links:
        if urlparse(link).netloc == urlparse(r.url).netloc and "#" in link:
            continue
        try:
            r2 = requests.get(link, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        ats = classify_url(r2.url)
        if ats:
            result.update(ats=ats, evidence=r2.url)
            return result
        ats, evidence = scan_text_for_ats(r2.text)
        if ats:
            result.update(ats=ats, evidence=evidence)
            return result

    # 4. No ATS found — classify the leftovers
    if mailto:
        result.update(ats="email", evidence=mailto[:200])
    elif len(r.text) < 5000:
        result.update(ats="js-page", evidence=f"html only {len(r.text)} bytes")
    else:
        domains = {urlparse(l).netloc for l in apply_links} - {urlparse(r.url).netloc}
        result["evidence"] = "; ".join(sorted(domains)) or "no external apply link"
    return result


def load_results_from_csv(csv_path):
    """Load a previous scan and overlay indeed_resolve.json refinements."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        results = list(csv.DictReader(f))

    if INDEED_RESOLVE_JSON.exists():
        refined = {r["job_id"]: r for r in json.loads(INDEED_RESOLVE_JSON.read_text())}
        n_overlaid = 0
        for row in results:
            ref = refined.get(row["job_id"])
            if ref and ref.get("indeed_verdict") in INDEED_VERDICT_TO_ATS:
                row["ats"] = INDEED_VERDICT_TO_ATS[ref["indeed_verdict"]]
                n_overlaid += 1
        print(f"覆寫 {n_overlaid} 筆 indeed CDP 解析結果（{INDEED_RESOLVE_JSON.name}）")
    return results


# scan_text_for_ats matches ATS domains anywhere in raw HTML, so evidence
# can be a script src (…successfactors.eu/…/jquery.js) or a footer link
# (join.com/terms). Such evidence still proves WHICH ats hosts the job,
# but must never be stored as the apply link.
_STATIC_ASSET_EXTS = (".js", ".css", ".map", ".json", ".png", ".jpg", ".jpeg",
                      ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf")
_JUNK_PATH_RE = re.compile(
    r"/(terms|privacy(-policy)?|legal|imprint|impressum|datenschutz|agb|"
    r"cookies?|cookie-richtlinie)(/|$|\?)", re.I)
_LOCALE_ONLY_PATH_RE = re.compile(r"^/?[a-z]{2}([_-][a-z]{2})?/?$", re.I)


def plausible_apply_url(url):
    """True when the URL could be a real apply page — rejects static assets,
    terms/privacy pages, and bare (or locale-only) homepages."""
    u = (url or "").strip()
    if u.startswith("mailto:"):
        return True
    if not u.startswith(("http://", "https://")):
        return False
    path = urlparse(u).path
    if path in ("", "/") or _LOCALE_ONLY_PATH_RE.match(path):
        return False
    if path.rstrip("/").lower().endswith(_STATIC_ASSET_EXTS):
        return False
    if _JUNK_PATH_RE.search(path):
        return False
    return True


def _evidence_to_apply_url(evidence):
    ev = (evidence or "").strip()
    return ev if plausible_apply_url(ev) else None


def write_results_to_db(results, checked_at, db_path=DB_PATH):
    """Store ats / apply_url / ats_checked_at on each scanned job."""
    conn = init_db(str(db_path))  # ensures the ats columns exist
    updated = missing = 0
    for r in results:
        ok = set_job_ats(
            conn, r["job_id"], r["ats"],
            apply_url=_evidence_to_apply_url(r.get("evidence")),
            checked_at=checked_at,
        )
        updated += ok
        missing += not ok
    conn.close()
    note = f"（{missing} 筆 job_id 已不在 DB）" if missing else ""
    print(f"已寫入 DB：{updated} 筆 ats/apply_url/ats_checked_at{note}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--write-db", action="store_true",
                        help="store ats/apply_url/ats_checked_at on the jobs table")
    parser.add_argument("--from-csv", metavar="PATH",
                        help="skip scanning; load results from a previous CSV")
    args = parser.parse_args()

    if args.from_csv:
        results = load_results_from_csv(args.from_csv)
        # the data is only as fresh as the scan that produced the file
        checked_at = datetime.fromtimestamp(
            Path(args.from_csv).stat().st_mtime
        ).strftime("%Y-%m-%dT%H:%M:%S")
        print(f"載入 {len(results)} 筆（{args.from_csv}，checked_at={checked_at}）")
    else:
        checked_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        jobs = fetch_jobs(args.limit)
        print(f"掃描 {len(jobs)} 筆德國境內 A + B≥70 職缺（workers={MAX_WORKERS}）...")
        if not jobs:
            return  # scheduled runs: an empty pool is a no-op, not an error

        results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(resolve_one, dict(j)): j for j in jobs}
            for i, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                results.append(res)
                print(f"  [{i}/{len(jobs)}] {res['ats']:<16} {res['company'][:30]} — {res['title'][:40]}")

        results.sort(key=lambda r: (r["ats"], r["company"]))
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
        print(f"明細已寫入 {OUT_CSV}")

    counts = Counter(r["ats"] for r in results)
    print(f"\n=== ATS 分佈（共 {len(results)} 筆）===")
    for ats, n in counts.most_common():
        print(f"  {ats:<16} {n:>3}  ({n / len(results):.0%})")
    unknown_domains = Counter(
        r["evidence"] for r in results if r["ats"] == "unknown" and r["evidence"]
    )
    if unknown_domains:
        print("\n=== unknown 的 apply link 網域 ===")
        for dom, n in unknown_domains.most_common(15):
            print(f"  {n:>2}  {dom[:100]}")

    if args.write_db:
        write_results_to_db(results, checked_at)


if __name__ == "__main__":
    main()
