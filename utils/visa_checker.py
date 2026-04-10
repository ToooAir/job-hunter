"""
utils/visa_checker.py — Chancenkarte-specific visa compatibility analysis.

The Phase 2 scorer assigns a coarse visa_restriction label (eu_only / open /
sponsored / unclear).  This module does a deeper on-demand analysis: it scans
the JD for the exact phrases that triggered that label, then asks the LLM to
reason about whether a Chancenkarte holder can still apply and how.
"""

import logging
import re

from utils.db import init_db, fetch_job_by_id, set_visa_analysis
from utils.llm import make_client, chat_model, rate_limit

log = logging.getLogger(__name__)

# ── Keyword scanner ────────────────────────────────────────────────────────────

_EXCLUDE_PATTERNS = [
    r"EU\s+citizens?\s+only",
    r"must\s+have\s+(the\s+)?right\s+to\s+work",
    r"right\s+to\s+work\s+in\s+Germany",
    r"valid\s+work\s+permit\s+required",
    r"eligible\s+to\s+work\s+without\s+sponsorship",
    r"no\s+visa\s+sponsorship",
    r"Arbeitserlaubnis\s+erforderlich",
    r"Aufenthaltstitel",
    r"legally\s+authorized\s+to\s+work",
    r"EU[- ]Bürger",
    r"Arbeitsgenehmigung",
]

_SPONSOR_PATTERNS = [
    r"visa\s+sponsorship\s+(provided|available|offered)",
    r"we\s+sponsor\s+work\s+permits?",
    r"support\s+with\s+visa",
    r"relocation\s+support\s+including\s+visa",
    r"help\s+with\s+work\s+auth",
    r"open\s+to\s+international\s+candidates",
    r"international\s+team",
    r"relocation\s+package",
]


def _scan_keywords(jd_text: str) -> dict:
    """Return lists of matched exclude/sponsor phrases found in the JD."""
    text = jd_text or ""
    found_exclude = [
        m.group(0) for pat in _EXCLUDE_PATTERNS
        for m in re.finditer(pat, text, re.IGNORECASE)
    ]
    found_sponsor = [
        m.group(0) for pat in _SPONSOR_PATTERNS
        for m in re.finditer(pat, text, re.IGNORECASE)
    ]
    return {"exclude": found_exclude, "sponsor": found_sponsor}


def _extract_visa_context(jd_text: str, max_chars: int = 2000) -> str:
    """Return only sentences containing visa-related keywords (±1 sentence context).

    Falls back to the first 2000 chars of the JD if no keywords are found,
    so the LLM always has *something* to reason about.
    """
    text = jd_text or ""
    if not text:
        return ""

    # Split into sentences on . ! ? or newlines
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    all_patterns = _EXCLUDE_PATTERNS + _SPONSOR_PATTERNS
    hit_indices: set[int] = set()
    for i, sent in enumerate(sentences):
        for pat in all_patterns:
            if re.search(pat, sent, re.IGNORECASE):
                # ±1 sentence context
                hit_indices.update([max(0, i - 1), i, min(len(sentences) - 1, i + 1)])
                break

    if not hit_indices:
        return text[:max_chars]

    # Reconstruct in original order, deduplicated
    selected = [sentences[i] for i in sorted(hit_indices)]
    result = " ".join(selected)
    return result[:max_chars]


# ── Prompt ─────────────────────────────────────────────────────────────────────

_LANG_INSTRUCTION = {
    "en": "Respond in English.",
    "zh": "Respond in Traditional Chinese (繁體中文).",
}

_SECTIONS = {
    "en": {
        "scan":        "### JD Scan",
        "scan_h":      "(List all work-permit-related phrases found in the JD, marking each as 'restrictive' or 'sponsor-friendly')",
        "compat":      "### Chancenkarte Compatibility",
        "compat_h":    """\
Choose one and explain:
- ✅ **Clearly friendly** — JD actively mentions relocation support / international candidates
- ⚠️ **Ambiguous** — JD requires right to work but does not explicitly exclude Chancenkarte
- 🔴 **Clearly restrictive** — JD explicitly requires EU citizenship or existing full work permit""",
        "advice":      "### Application Advice",
        "advice_h":    "(Whether it's worth applying and what steps to take)",
        "cover":       "### Cover Letter Tips",
        "cover_h":     "(How to proactively address Chancenkarte status; 1–2 example sentences)",
        "questions":   "### Questions to Ask HR",
        "questions_h": "(1–2 specific questions to confirm at first contact)",
    },
    "zh": {
        "scan":        "### JD 原文掃描",
        "scan_h":      "（列出 JD 中與工作許可相關的原始字句，並標注是「限制性」還是「友善性」語言）",
        "compat":      "### Chancenkarte 相容性判斷",
        "compat_h":    """\
從以下選一個，並說明原因：
- ✅ **明確友善** — JD 主動提到 relocation support / international candidates
- ⚠️ **模糊地帶** — JD 要求 right to work 但未明確排除 Chancenkarte
- 🔴 **明確限制** — JD 明確要求 EU 公民或現有完整工作許可，Chancenkarte 不符合""",
        "advice":      "### 投遞建議",
        "advice_h":    "（具體說明是否值得投遞、需要採取哪些步驟）",
        "cover":       "### 求職信補充建議",
        "cover_h":     "（建議在 Cover Letter 或 Email 中如何主動說明 Chancenkarte 身份，提供 1–2 句範例）",
        "questions":   "### 建議向 HR 詢問的問題",
        "questions_h": "（1–2 個具體問題，適合在第一次聯絡時確認）",
    },
}

_PROMPT_TEMPLATE = """\
You are a German immigration and hiring specialist advising a candidate who holds a \
**Chancenkarte** (Opportunity Card) issued under §20a AufenthG.

Key facts about the Chancenkarte:
- It is a legal residence permit for job-seeking in Germany (up to 2 years).
- Holders can work up to 20 hours/week in any job while searching, and start a \
  trial/probationary work of up to 2 weeks without a full work permit.
- Once a qualifying job is found, holders apply for a full work permit (Fachkräfte-Visum \
  or Niederlassungserlaubnis path depending on qualifications).
- Many "right to work" clauses in JDs are written for non-EU applicants generally; \
  companies often do not realise the Chancenkarte counts as a legal basis.

{lang_instruction} Use Markdown format, with these exact sections:

{scan}
{scan_h}

{compat}
{compat_h}

{advice}
{advice_h}

{cover}
{cover_h}

{questions}
{questions_h}

---
Company: {company}
Title: {title}
Current visa_restriction classification: {visa_restriction}
Keywords found in JD — restrictive: {exclude_keywords}
Keywords found in JD — sponsor-friendly: {sponsor_keywords}

Relevant JD passages (visa-related context only):
{visa_context}
"""


def analyze_visa_compatibility(job_id: str, db_path: str, lang: str = "en") -> str | None:
    """Generate and persist a Chancenkarte compatibility analysis. Returns text or None."""
    conn = init_db(db_path)
    job = fetch_job_by_id(conn, job_id)
    if job is None:
        conn.close()
        log.warning("analyze_visa_compatibility: job %s not found", job_id)
        return None

    jd_text = job.get("raw_jd_text") or ""
    keywords = _scan_keywords(jd_text)
    visa_context = _extract_visa_context(jd_text)

    s = _SECTIONS.get(lang, _SECTIONS["en"])
    no_match = "（無）" if lang == "zh" else "(none)"
    prompt = _PROMPT_TEMPLATE.format(
        lang_instruction=_LANG_INSTRUCTION.get(lang, _LANG_INSTRUCTION["en"]),
        scan=s["scan"],
        scan_h=s["scan_h"],
        compat=s["compat"],
        compat_h=s["compat_h"],
        advice=s["advice"],
        advice_h=s["advice_h"],
        cover=s["cover"],
        cover_h=s["cover_h"],
        questions=s["questions"],
        questions_h=s["questions_h"],
        company=job["company"],
        title=job["title"],
        visa_restriction=job.get("visa_restriction") or "unclear",
        exclude_keywords=", ".join(keywords["exclude"]) or no_match,
        sponsor_keywords=", ".join(keywords["sponsor"]) or no_match,
        visa_context=visa_context,
    )

    client = make_client()
    rate_limit()
    try:
        resp = client.chat.completions.create(
            model=chat_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        analysis = resp.choices[0].message.content.strip()
        set_visa_analysis(conn, job_id, analysis)
        conn.close()
        log.info(
            "visa analysis generated for %s @ %s (visa_restriction=%s)",
            job["title"], job["company"], job.get("visa_restriction"),
        )
        return analysis
    except Exception as exc:
        log.error("Failed to analyze visa compatibility for job %s: %s", job_id, exc)
        conn.close()
        return None
