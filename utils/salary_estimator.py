"""
utils/salary_estimator.py — LLM-based salary estimation for German tech roles.

No external APIs needed. Uses LLM market knowledge + JD context.
External reference sites (Glassdoor, Kununu, Levels.fyi) are surfaced as
link buttons in the dashboard — not scraped (require auth).
"""

import logging
from datetime import datetime, timezone

from utils.db import init_db, fetch_job_by_id, set_salary_estimate
from utils.llm import make_client, chat_model, rate_limit
from utils.levels_scraper import fetch_levels_data, LevelsSummary

log = logging.getLogger(__name__)

_LANG_INSTRUCTION = {
    "en": "Respond in English.",
    "zh": "Respond in Traditional Chinese (繁體中文).",
}

_SECTIONS = {
    "en": {
        "salary":      "### Salary Estimate",
        "market":      "- **Market range**: €X,000 – €Y,000 / year (gross annual)",
        "jd_sal":      "- **JD-stated salary**: {jd_salary}",
        "conf":        "- **Confidence**: High / Medium / Low",
        "basis":       "- **Basis** (2–3 points): seniority, company size, location, industry",
        "nego":        "### Negotiation Tips",
        "ask":         "- **Opening ask**: €X,000 (typically upper end of range)",
        "floor":       "- **Floor**: €X,000",
        "strat":       "- **Strategy** (2–3 points): actionable negotiation advice for German job market",
        "form":        "### Gehaltsvorstellung — Application Form",
        "form_fig":    "- **Suggested figure**: €X,000",
        "form_note":   "- **Why lower than opening ask**: 1 sentence — avoids budget filters while staying above midpoint",
        "form_phrase": '- **Phrasing**: e.g. "My salary expectation is €X,000 gross per year."',
    },
    "zh": {
        "salary":      "### 薪資估計",
        "market":      "- **市場區間**：€X,000 – €Y,000 / year（gross，年薪稅前）",
        "jd_sal":      "- **JD 標示薪資**：{jd_salary}",
        "conf":        "- **信心水準**：高 / 中 / 低",
        "basis":       "- **估計依據**（2–3 點）：職位層級、公司規模、地點、產業",
        "nego":        "### 談判建議",
        "ask":         "- **開價建議**：€X,000（通常為區間上緣）",
        "floor":       "- **底線建議**：€X,000",
        "strat":       "- **策略**（2–3 點）：具體可操作的德國求職薪資談判建議",
        "form":        "### Gehaltsvorstellung — 表單填寫",
        "form_fig":    "- **建議填寫數字**：€X,000",
        "form_note":   "- **為何低於談判開價**：1 句說明 — 避免被預算篩除，同時仍高於市場中位數",
        "form_phrase": "- **建議用語**：例如「My salary expectation is €X,000 gross per year.」",
    },
}

_PROMPT_TEMPLATE = """\
You are a compensation specialist familiar with tech compensation in {location}.
Based on the job details below, estimate the market salary range, provide negotiation guidance, \
and give a concrete single figure suitable for writing in a job application form (Gehaltsvorstellung).
{lang_instruction}
Output in Markdown format.

{levels_section}\
{salary_heading}
{market}
{jd_sal}
{conf}
{basis}

{nego_heading}
{ask}
{floor}
{strat}

{form_heading}
{form_fig}
{form_note}
{form_phrase}

---
Rules:
- All figures are gross annual EUR for the job location.
- If the role is remote/outside major cities, adjust downward 5–15%.
- Apply city-tier adjustments based on the job location: major tech hubs typically command a premium; tier-2 or regional cities: -5 to -15% lower.
- Mark uncertain estimates with（估計）if responding in Chinese, or "(est.)" if in English.
- Do not invent specific internal company pay data.
- Negotiation opening ask = upper end of market range.
- Gehaltsvorstellung figure = 65–75th percentile of market range (lower than opening ask to pass
  ATS/HR budget filters, but above midpoint to avoid undervaluing the candidate).
  Provide one specific number, not a range.
- If "Recent Market Reference Data" is provided above, use it to calibrate your range.
  Prioritise entries from the past 12 months. Note currency conversions if data is in USD.

Job details:
Company: {company}
Title: {title}
Location: {location}
Contract type: {contract_type}
Source: {source}

Job description (excerpt):
{jd_text}
"""


def _fmt_summary_row(result: dict) -> str | None:
    """Format one LevelsResult as a markdown table row. Returns None if no data."""
    s = result.get("summary")
    if not s or not s.get("median_total"):
        return None
    loc   = result.get("source_slug", "—")
    med   = f"€{s['median_total']:,}"
    p25   = f"€{s['p25']:,}" if s.get("p25")  else "—"
    p75   = f"€{s['p75']:,}" if s.get("p75")  else "—"
    p90   = f"€{s['p90']:,}" if s.get("p90")  else "—"
    fx    = f" *({s['fx_note']})*" if s.get("fx_note") else ""
    return f"| {loc} | {med}{fx} | {p25} | {p75} | {p90} |"


def _build_levels_section(levels_results: list[dict]) -> str:
    """
    Build a multi-location comparison table from all fetched Levels.fyi layers.
    Returns '' if no usable data is available.
    """
    if not levels_results:
        return ""

    rows = [_fmt_summary_row(r) for r in levels_results]
    rows = [r for r in rows if r]
    if not rows:
        return ""

    fetched_dates = []
    for r in levels_results:
        try:
            fetched_dates.append(datetime.fromisoformat(r["fetched_at"]).strftime("%Y-%m-%d"))
        except Exception:
            pass
    fetched_label = f"fetched {min(fetched_dates)}" if fetched_dates else ""

    lines = [
        f"### Market Reference Data (Levels.fyi, {fetched_label})",
        f"_(total compensation, gross annual EUR)_",
        f"| Location | Median | P25 | P75 | P90 |",
        f"|----------|--------|-----|-----|-----|",
    ] + rows + [""]

    return "\n".join(lines) + "\n"


def estimate_salary(job_id: str, db_path: str, lang: str = "en") -> str | None:
    """Generate and persist a salary estimate + negotiation brief. Returns text or None."""
    conn = init_db(db_path)
    job = fetch_job_by_id(conn, job_id)
    if job is None:
        conn.close()
        log.warning("estimate_salary: job %s not found", job_id)
        return None

    s = _SECTIONS.get(lang, _SECTIONS["en"])
    jd_salary = job.get("salary_range") or ("未標示" if lang == "zh" else "not stated")

    # Fetch Levels.fyi market reference data (cached; silent fallback on failure)
    levels_results = fetch_levels_data(
        job_title=job["title"],
        job_location=job.get("location"),
        contract_type=job.get("contract_type"),
    )
    levels_section = _build_levels_section(levels_results)
    if levels_section:
        slugs = [r["source_slug"] for r in levels_results]
        log.info("salary estimate: injecting Levels.fyi data %s for job %s", slugs, job_id)

    prompt = _PROMPT_TEMPLATE.format(
        levels_section=levels_section,  # '' when no data
        lang_instruction=_LANG_INSTRUCTION.get(lang, _LANG_INSTRUCTION["en"]),
        salary_heading=s["salary"],
        market=s["market"],
        jd_sal=s["jd_sal"].format(jd_salary=jd_salary),
        conf=s["conf"],
        basis=s["basis"],
        nego_heading=s["nego"],
        ask=s["ask"],
        floor=s["floor"],
        strat=s["strat"],
        form_heading=s["form"],
        form_fig=s["form_fig"],
        form_note=s["form_note"],
        form_phrase=s["form_phrase"],
        company=job["company"],
        title=job["title"],
        location=job.get("location") or "Germany",
        contract_type=job.get("contract_type") or "unknown",
        source=job.get("source") or "—",
        jd_text=(job.get("raw_jd_text") or "")[:4000],
    )

    client = make_client()
    rate_limit()
    try:
        resp = client.chat.completions.create(
            model=chat_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        result = resp.choices[0].message.content.strip()
        set_salary_estimate(conn, job_id, result)
        conn.close()
        log.info("salary estimate generated for %s @ %s", job["title"], job["company"])
        return result
    except Exception as exc:
        log.error("Failed to estimate salary for job %s: %s", job_id, exc)
        conn.close()
        return None
