"""
utils/salary_estimator.py — LLM-based salary estimation for German tech roles.

No external APIs needed. Uses LLM market knowledge + JD context.
External reference sites (Glassdoor, Kununu, Levels.fyi) are surfaced as
link buttons in the dashboard — not scraped (require auth).
"""

import logging

from utils.db import init_db, fetch_job_by_id, set_salary_estimate
from utils.llm import make_client, chat_model, rate_limit

log = logging.getLogger(__name__)

_LANG_INSTRUCTION = {
    "en": "Respond in English.",
    "zh": "Respond in Traditional Chinese (繁體中文).",
}

_SECTIONS = {
    "en": {
        "salary":  "### Salary Estimate",
        "market":  "- **Market range**: €X,000 – €Y,000 / year (gross annual)",
        "jd_sal":  "- **JD-stated salary**: {jd_salary}",
        "conf":    "- **Confidence**: High / Medium / Low",
        "basis":   "- **Basis** (2–3 points): seniority, company size, location, industry",
        "nego":    "### Negotiation Tips",
        "ask":     "- **Opening ask**: €X,000 (typically upper end of range)",
        "floor":   "- **Floor**: €X,000",
        "strat":   "- **Strategy** (2–3 points): actionable negotiation advice for German job market",
    },
    "zh": {
        "salary":  "### 薪資估計",
        "market":  "- **市場區間**：€X,000 – €Y,000 / year（gross，年薪稅前）",
        "jd_sal":  "- **JD 標示薪資**：{jd_salary}",
        "conf":    "- **信心水準**：高 / 中 / 低",
        "basis":   "- **估計依據**（2–3 點）：職位層級、公司規模、地點、產業",
        "nego":    "### 談判建議",
        "ask":     "- **開價建議**：€X,000（通常為區間上緣）",
        "floor":   "- **底線建議**：€X,000",
        "strat":   "- **策略**（2–3 點）：具體可操作的德國求職薪資談判建議",
    },
}

_PROMPT_TEMPLATE = """\
You are a compensation specialist for the German tech industry.
Based on the job details below, estimate the market salary range and provide negotiation guidance.
{lang_instruction}
Output in Markdown format.

{salary_heading}
{market}
{jd_sal}
{conf}
{basis}

{nego_heading}
{ask}
{floor}
{strat}

---
Rules:
- All figures are gross annual EUR for Germany.
- If the role is remote/outside major cities, adjust downward 5–15%.
- Berlin/Hamburg/Munich premium: standard. Tier-2 cities: -5 to -10%.
- Mark uncertain estimates with（估計）if responding in Chinese, or "(est.)" if in English.
- Do not invent specific internal company pay data.

Job details:
Company: {company}
Title: {title}
Location: {location}
Contract type: {contract_type}
Source: {source}

Job description (excerpt):
{jd_text}
"""


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

    prompt = _PROMPT_TEMPLATE.format(
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
