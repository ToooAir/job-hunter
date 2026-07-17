#!/usr/bin/env python3
"""remote_geo_triage.py — relabel locations the Germany filters cannot see.

The downstream Germany filters (apply_queue.GERMANY_KEYWORDS,
ats_scan.GERMANY_LIKE) are a 14-keyword LIKE list. Two whole classes of
jobs silently fall out of the apply queue because of that. This stage runs
on un-scored AND scored jobs, BEFORE phase2_scorer: its rule verdicts cost
nothing, and a "Remote — non-EU" label lets the scorer skip the job before
any LLM spend (see phase2_scorer.geo_excluded).

Pass 0 — German locations the keywords miss. Second-tier cities
("Nuremberg", "Karlsruhe"), "(DE)" suffixes ("Dresden (DE)"), postal-code
forms ("54595 Prüm"), small towns and "Bundesweit". geo_de.is_germany_location
vets them (any non-DE country/city marker vetoes), then ", Germany" is
appended so the existing keyword filters match with zero downstream changes.

Pass 1/2 — bare-"Remote" jobs from global boards (remotive/jobicy/
weworkremotely/wttj), plus variants ("Anywhere in the World", "Remote / X"),
classified by Germany-hiring eligibility from the JD text:

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
(default: apply_queue.MIN_B_SCORE, so every queue-grade job gets classified)
to the LLM via the shared apply_llm plumbing. Un-scored jobs have no
match_score yet, so the LLM gate naturally skips them — they are picked up
by a later --llm run once scored. German-language JDs may miss the English
rule patterns before phase2 adds translated_jd_text; they stay bare 'Remote'
and get a second rule pass (with translation) on the next daily run.

Usage:
    python remote_geo_triage.py                  # dry run, rules only
    python remote_geo_triage.py --write-db       # rules, write labels
    python remote_geo_triage.py --write-db --llm # + LLM pass on unclear B65+
"""

import argparse
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils.apply_queue import GERMANY_KEYWORDS, MIN_B_SCORE  # noqa: E402 — pure stdlib
from utils.geo_de import is_germany_location  # noqa: E402

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

# Location strings that mean hire-from-anywhere — Germany-eligible by
# definition, no JD evidence needed. Matched on the exact (lowered) string
# only: the word "anywhere" inside a JD is NOT trusted ("work anywhere in
# the US" would slip through classify_rules).
WORLDWIDE_LOCATIONS = {
    "anywhere", "anywhere in the world", "work from anywhere",
    "worldwide", "remote worldwide", "remote (worldwide)",
}

GERMANY_RE = re.compile(
    r"\b(germany|deutschland|german entity|hamburg|berlin|munich|münchen|"
    r"cologne|köln|frankfurt|stuttgart|düsseldorf|leipzig|hannover)\b", re.I)
# Hiring-context phrases only. A bare "worldwide" is NOT one: marketing copy
# ("customers worldwide", "millions of people worldwide") sits in almost every
# JD and mislabeled US-only jobs as Germany-eligible. "from anywhere" is only
# trusted when no region qualifier follows ("work from anywhere across US,
# Canada or Mexico" is a residency restriction, not hire-from-anywhere).
WORLDWIDE_RE = re.compile(
    r"\b(anywhere in the world"
    r"|from anywhere(?!\s+(?:across|in|within)\b)"
    r"|(?:hire|hiring|work|working|remote)\s+worldwide"
    r"|globally distributed team"
    r"|any country|any location)\b", re.I)
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
    """Bare 'Remote' plus variants ("Remote / X", "Anywhere in the World").

    The triage labels ('Remote — …') use an em-dash, so they never match the
    'Remote /%' / 'Remote, %' patterns — labelled jobs stay out of the pool.
    """
    ww = ",".join("?" * len(WORLDWIDE_LOCATIONS))
    sql = (
        "SELECT id, company, title, location, match_score, "
        "       COALESCE(title,'') || ' ' || COALESCE(raw_jd_text,'') || ' ' "
        "       || COALESCE(translated_jd_text,'') AS jd "
        "FROM jobs WHERE status IN ('un-scored','scored') AND ("
        "    location='Remote' OR location LIKE 'Remote /%' "
        f"   OR location LIKE 'Remote, %' OR LOWER(location) IN ({ww})"
        ") ORDER BY match_score DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, tuple(WORLDWIDE_LOCATIONS))]


def _matches_germany_keywords(location: str) -> bool:
    low = (location or "").lower()
    return any(k.lower() in low for k in GERMANY_KEYWORDS)


def fetch_de_candidates(conn, limit=None):
    """Pass 0 pool: scored jobs the 14-keyword Germany filter does not match.

    Remote-ish locations are excluded — they belong to the remote pass, and
    substring city matching on them backfires ("Remote / Lisbonne" contains
    "bonn").
    """
    sql = (
        "SELECT id, company, title, location, match_score "
        "FROM jobs WHERE status IN ('un-scored','scored') "
        "AND location IS NOT NULL AND location != '' "
        "AND location NOT LIKE 'Remote%' "
        "ORDER BY match_score DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)
            if not _matches_germany_keywords(r["location"])
            and r["location"].strip().lower() not in WORLDWIDE_LOCATIONS]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-db", action="store_true",
                        help="write the new location labels back to the jobs table")
    parser.add_argument("--llm", action="store_true",
                        help="LLM pass on rule-unclear jobs (score gate below)")
    # Aligned with the queue's B floor: any job good enough to draft is worth
    # one cheap classify call. 80 (until 2026-07-17) left B65-79 bare-Remote
    # jobs permanently queue-ineligible.
    parser.add_argument("--llm-min-score", type=int, default=MIN_B_SCORE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    relabel: list[tuple[str, str, str]] = []  # (new_location, job_id, old_location)

    # ── Pass 0: German locations the 14-keyword filter misses ─────────────
    de_jobs = fetch_de_candidates(conn, args.limit)
    de_hits = [j for j in de_jobs if is_germany_location(j["location"])]
    print(f"分類 Pass 0:{len(de_jobs)} 筆非德國標記地點中,{len(de_hits)} 筆"
          f"判定在德國 → 加註 ', Germany'", flush=True)
    for j in de_hits:
        relabel.append((f"{j['location']}, Germany", j["id"], j["location"]))
        print(f"  ✓ {j['match_score'] or 0:>3} {(j['company'] or '')[:30]}"
              f" — {j['location'][:45]}", flush=True)

    # ── Pass 1/2: Remote pool (rules, then optional LLM) ──────────────────
    jobs = fetch_remote_jobs(conn, args.limit)
    print(f"分類 {len(jobs)} 筆 Remote 類 scored 職缺(rules"
          f"{' + LLM' if args.llm else ''}, write={args.write_db})", flush=True)

    client = model = None
    llm_calls = llm_errors = consecutive_errors = 0
    llm_enabled = args.llm
    counts: Counter = Counter()

    for job in jobs:
        loc = (job["location"] or "").strip()
        if loc.lower() in WORLDWIDE_LOCATIONS:
            region = "germany"  # the location string itself says anywhere
        else:
            # variants like "Remote / Berlin" carry the signal in the label
            region = classify_rules(f"{loc} {job['jd']}")
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
            relabel.append((LABELS[region], job["id"], job["location"]))
            if region == "germany":
                print(f"  ✓ Germany-eligible: {job['match_score'] or 0:>3} "
                      f"{(job['company'] or '')[:30]} — {(job['title'] or '')[:45]}",
                      flush=True)
        elif llm_said_unclear:
            # persist the verdict so the next --llm run skips this job
            counts["llm_unclear"] += 1
            relabel.append((LLM_UNCLEAR_LABEL, job["id"], job["location"]))

    print(f"\n=== 分類結果(Pass 0 加註 {len(de_hits)} 筆;Remote 池 "
          f"{len(jobs)} 筆,LLM 呼叫 {llm_calls} 次,失敗 {llm_errors} 次)===")
    for region in ("germany", "europe", "non_eu", "unclear"):
        n = counts.get(region, 0)
        label = LABELS.get(region, "(不變)")
        print(f"  {region:<8} {n:>4}  → {label}")
    if counts.get("llm_unclear"):
        print(f"  (其中 LLM 判 unclear {counts['llm_unclear']} 筆 → "
              f"{LLM_UNCLEAR_LABEL},下次 --llm 不再重判)")

    if args.write_db and relabel:
        # matching the original location keeps the update idempotent and
        # race-safe (a job relabelled in the meantime is left alone)
        cur = conn.executemany(
            "UPDATE jobs SET location=? WHERE id=? AND location=?", relabel)
        conn.commit()
        print(f"已寫入 DB:{cur.rowcount} 筆 location 重標")
    conn.close()


if __name__ == "__main__":
    main()
