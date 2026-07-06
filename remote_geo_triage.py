#!/usr/bin/env python3
"""remote_geo_triage.py — classify bare-"Remote" jobs by Germany-hiring eligibility.

Scrapers of global remote boards (remotive/jobicy/weworkremotely/wttj) label
jobs with a bare location="Remote", which the Germany filters downstream
(apply_queue.GERMANY_KEYWORDS, ats_scan.GERMANY_LIKE) can neither include nor
exclude — the whole bucket silently falls out of the apply queue.

This stage relabels location on status='scored' jobs where location='Remote':

    Remote — Germany   JD says Germany-eligible (or hire-from-anywhere)
    Remote — EU        Europe/EU-wide remote; Germany plausible, human decides
    Remote — non-EU    restricted to US/Canada/UK/other non-EU regions
    Remote — unclear   the LLM looked and could not tell — marked so the next
                       --llm run does not pay for the same job again
    (unchanged)        rules found no signal and no LLM pass ran (a later
                       --llm run may still classify it)

"Remote — Germany" contains "German", so the existing GERMANY keyword filters
pick those jobs up with zero downstream changes; the other labels keep them
out, which is the current (implicit) behaviour made explicit.

Pass 1 is regex rules over the JD text (free, runs on the whole bucket).
Pass 2 (--llm) sends still-unclear jobs with match_score >= --llm-min-score
to the LLM via the shared apply_llm plumbing.

Usage:
    python remote_geo_triage.py                  # dry run, rules only
    python remote_geo_triage.py --write-db       # rules, write labels
    python remote_geo_triage.py --write-db --llm # + LLM pass on unclear 80+
"""

import argparse
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "jobs.db"

LABELS = {
    "germany": "Remote — Germany",
    "europe": "Remote — EU",
    "non_eu": "Remote — non-EU",
}
# An LLM "unclear" verdict is itself a result worth persisting: the label keeps
# the job out of the Germany filters (no "German" substring) and out of the
# next --llm run's bill. Transient LLM *failures* stay bare 'Remote' instead,
# so they are retried.
LLM_UNCLEAR_LABEL = "Remote — unclear"

GERMANY_RE = re.compile(
    r"\b(germany|deutschland|german entity|hamburg|berlin|munich|münchen|"
    r"cologne|köln|frankfurt|stuttgart|düsseldorf|leipzig|hannover)\b", re.I)
WORLDWIDE_RE = re.compile(
    r"\b(work from anywhere|anywhere in the world|from anywhere|worldwide"
    r"|globally distributed|any country|any location)\b", re.I)
EU_RE = re.compile(
    r"\b(europe|european union|eu-based|based in the eu|within the eu|eea|"
    r"cet|cest|central european time)\b", re.I)
NON_EU_RE = re.compile(
    r"\b(us only|usa only|us-based|based in the (us|usa|u\.s\.|united states)|"
    r"(located|residing|reside|live|living) in the (us|usa|u\.s\.|united states)|"
    r"us citizens?|green card|authorized to work in the (us|usa|u\.s\.|united states)|"
    r"us work (authorization|permit)|north america(n)? (only|based|time ?zones?)|"
    r"(pst|pt|est|et) time ?zones?|west coast|east coast|bay area|"
    r"uk only|uk-based|canada only|canadian residents)\b",
    re.I)


def classify_rules(text: str) -> str:
    """Return 'germany' | 'europe' | 'non_eu' | 'unclear'.

    A single unambiguous signal decides; conflicting signals (e.g. a JD that
    names both Germany and a US restriction) stay 'unclear' for the LLM pass.
    """
    de = bool(GERMANY_RE.search(text))
    ww = bool(WORLDWIDE_RE.search(text))
    eu = bool(EU_RE.search(text))
    non = bool(NON_EU_RE.search(text))
    if de and not non:
        return "germany"
    if ww and not (de or non):
        return "germany"  # hire-from-anywhere includes Germany
    if eu and not (de or non):
        return "europe"
    if non and not (de or eu or ww):
        return "non_eu"
    return "unclear"


_LLM_SYSTEM = (
    "You classify remote job postings by where the candidate must live to be "
    "hired. Answer strict JSON: {\"region\": \"germany\"|\"europe\"|\"non_eu\""
    "|\"unclear\", \"evidence\": \"<short quote from the posting>\"}.\n"
    "germany: a candidate living in Germany can be hired (Germany named, EU "
    "country list including Germany, or hire-from-anywhere).\n"
    "europe: Europe/EU-wide eligibility where Germany is plausible but not "
    "named.\n"
    "non_eu: restricted to regions that exclude Germany (US/Canada/UK/etc. "
    "only, or non-European time-zone requirements).\n"
    "unclear: the posting does not say. Never guess beyond the text."
)


def classify_llm(client, model, job) -> str:
    from utils.apply_llm import _chat_json, _sanitize
    text = _sanitize((job["jd"] or "")[:6000])
    user = (f"Company: {_sanitize(job['company'])}\n"
            f"Title: {_sanitize(job['title'])}\n\nJob posting:\n{text}")
    out = _chat_json(client, model, _LLM_SYSTEM, user, max_tokens=200)
    region = (out or {}).get("region", "unclear")
    return region if region in ("germany", "europe", "non_eu") else "unclear"


def fetch_remote_jobs(conn, limit=None):
    sql = (
        "SELECT id, company, title, match_score, "
        "       COALESCE(title,'') || ' ' || COALESCE(raw_jd_text,'') || ' ' "
        "       || COALESCE(translated_jd_text,'') AS jd "
        "FROM jobs WHERE status='scored' AND location='Remote' "
        "ORDER BY match_score DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-db", action="store_true",
                        help="write the new location labels back to the jobs table")
    parser.add_argument("--llm", action="store_true",
                        help="LLM pass on rule-unclear jobs (score gate below)")
    parser.add_argument("--llm-min-score", type=int, default=80)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    jobs = fetch_remote_jobs(conn, args.limit)
    print(f"分類 {len(jobs)} 筆 location='Remote' 的 scored 職缺(rules"
          f"{' + LLM' if args.llm else ''}, write={args.write_db})", flush=True)

    client = model = None
    llm_calls = llm_errors = consecutive_errors = 0
    llm_enabled = args.llm
    counts: Counter = Counter()
    relabel: list[tuple[str, str]] = []  # (new_location, job_id)

    for job in jobs:
        region = classify_rules(job["jd"])
        llm_said_unclear = False
        if region == "unclear" and llm_enabled and (job["match_score"] or 0) >= args.llm_min_score:
            if client is None:
                from utils.apply_llm import _defaults
                client, model = _defaults(None, None)
            # A transient API failure costs one job (stays bare 'Remote', so
            # the next --llm run retries it), never the batch — the rule-pass
            # relabels must still reach the DB write.
            try:
                region = classify_llm(client, model, job)
                llm_calls += 1
                consecutive_errors = 0
                llm_said_unclear = region == "unclear"
            except Exception as e:  # noqa: BLE001 — 429s etc. from the SDK
                llm_errors += 1
                consecutive_errors += 1
                print(f"  ⚠ LLM 失敗({type(e).__name__}),此筆保持 unclear:"
                      f" {job['company'][:30]}", flush=True)
                if consecutive_errors >= 3:
                    llm_enabled = False
                    print("  ⚠ 連續 3 次 LLM 失敗 → 熔斷,其餘只跑規則", flush=True)
                else:
                    time.sleep(10 * consecutive_errors)
        counts[region] += 1
        if region in LABELS:
            relabel.append((LABELS[region], job["id"]))
        elif llm_said_unclear:
            # persist the verdict so the next --llm run skips this job
            counts["llm_unclear"] += 1
            relabel.append((LLM_UNCLEAR_LABEL, job["id"]))
            if region == "germany":
                print(f"  ✓ Germany-eligible: {job['match_score']:>3} "
                      f"{job['company'][:30]} — {job['title'][:45]}", flush=True)

    print(f"\n=== 分類結果(共 {len(jobs)} 筆,LLM 呼叫 {llm_calls} 次"
          f",失敗 {llm_errors} 次)===")
    for region in ("germany", "europe", "non_eu", "unclear"):
        n = counts.get(region, 0)
        label = LABELS.get(region, "(不變)")
        print(f"  {region:<8} {n:>4}  → {label}")
    if counts.get("llm_unclear"):
        print(f"  (其中 LLM 判 unclear {counts['llm_unclear']} 筆 → "
              f"{LLM_UNCLEAR_LABEL},下次 --llm 不再重判)")

    if args.write_db and relabel:
        # location='Remote' guard keeps the update idempotent and race-safe
        cur = conn.executemany(
            "UPDATE jobs SET location=? WHERE id=? AND location='Remote'", relabel)
        conn.commit()
        print(f"已寫入 DB:{cur.rowcount} 筆 location 重標")
    conn.close()


if __name__ == "__main__":
    main()
