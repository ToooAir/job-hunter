"""
Phase 2 — Fit Scorer
RAG-augmented LLM scoring of un-scored jobs in the SQLite database.
"""

import json
import logging
import os
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal

import openai
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

from utils.db import init_db, get_unscored_jobs, update_score, fetch_job_by_id, reset_to_unscored, mark_error, mark_expired, set_interview_brief

# ── Setup ──────────────────────────────────────────────────────────────────────

load_dotenv()

DB_PATH     = os.getenv("DB_PATH", "./data/jobs.db")
QDRANT_PATH = os.getenv("QDRANT_PATH", "./qdrant_data")

from utils.llm import make_client, chat_model, emb_model, LLM_PROVIDER

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Pydantic Output Schema ─────────────────────────────────────────────────────


class ScoringResult(BaseModel):
    jd_language_req: Literal["en_required", "de_plus", "de_required", "unknown"]
    visa_restriction: Literal["open", "eu_only", "sponsored", "unclear"]
    salary_range: str  # extracted text or ""
    contract_type: Literal["permanent", "contract", "freelance", "unknown"]
    match_score: int  # 0–100
    fit_grade: Literal["A", "B", "C"]
    top_3_reasons: list[str]
    cover_letter_draft: str

    @field_validator("fit_grade", mode="before")
    @classmethod
    def derive_grade(cls, v, info):
        score = info.data.get("match_score", 0)
        lang = info.data.get("jd_language_req", "unknown")
        if lang == "de_required" or score < 60:
            return "C"
        if score >= 80:
            return "A"
        return "B"

    @field_validator("top_3_reasons")
    @classmethod
    def check_reasons_length(cls, v):
        assert len(v) == 3, "top_3_reasons must have exactly 3 items"
        return [r[:80] for r in v]

    @field_validator("cover_letter_draft")
    @classmethod
    def enforce_word_limit(cls, v):
        words = v.split()
        if len(words) <= 400:
            return v
        log.warning(
            "cover_letter_draft exceeded 400 words (%d); truncating to sentence boundary",
            len(words),
        )
        candidate = " ".join(words[:400])
        # Find the last sentence-ending punctuation in the truncated text.
        # Require the cut-point to be after ~150 words (≈ 900 chars) so we
        # don't accidentally produce a very short cover letter.
        MIN_CHARS = 900
        last_punct = max(
            candidate.rfind("."),
            candidate.rfind("。"),
            candidate.rfind("!"),
            candidate.rfind("！"),
            candidate.rfind("?"),
            candidate.rfind("？"),
        )
        if last_punct >= MIN_CHARS:
            return candidate[: last_punct + 1].strip()
        return candidate  # fallback: word boundary (no sentence end found)


def _parse_with_structured_output(
    client: openai.OpenAI, system_prompt: str, user_prompt: str
) -> ScoringResult:
    """Use OpenAI/Azure Structured Outputs (.parse). Raises if unsupported."""
    response = client.beta.chat.completions.parse(
        model=chat_model(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format=ScoringResult,
        temperature=0.3,
    )
    return response.choices[0].message.parsed


def _parse_with_json_mode(
    client: openai.OpenAI, system_prompt: str, user_prompt: str
) -> ScoringResult:
    """Fallback: JSON mode + manual Pydantic parse (for custom endpoints)."""
    json_instruction = (
        "\n\nRespond ONLY with valid JSON matching this schema: "
        "{jd_language_req, match_score, fit_grade, top_3_reasons, cover_letter_draft}"
    )
    response = client.chat.completions.create(
        model=chat_model(),
        messages=[
            {"role": "system", "content": system_prompt + json_instruction},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    raw = response.choices[0].message.content
    return ScoringResult.model_validate_json(raw)


def _call_llm(
    client: openai.OpenAI, system_prompt: str, user_prompt: str
) -> ScoringResult:
    """
    Try Structured Outputs first; fall back to JSON mode for custom endpoints
    that don't implement the /parse extension.
    """
    if LLM_PROVIDER == "custom":
        return _parse_with_json_mode(client, system_prompt, user_prompt)
    try:
        return _parse_with_structured_output(client, system_prompt, user_prompt)
    except Exception as exc:
        log.warning("Structured Outputs failed (%s) — retrying with JSON mode", exc)
        return _parse_with_json_mode(client, system_prompt, user_prompt)


# ── Retriever ──────────────────────────────────────────────────────────────────

COLLECTION = "candidate_kb"


def check_kb_ready(qdrant_path: str) -> bool:
    """Return True if the candidate_kb collection exists and has points."""
    try:
        from qdrant_client import QdrantClient
        qdrant = QdrantClient(path=qdrant_path)
        existing = {c.name for c in qdrant.get_collections().collections}
        if COLLECTION not in existing:
            return False
        info = qdrant.get_collection(COLLECTION)
        return (info.points_count or 0) > 0
    except Exception:
        return False


def check_kb_fresh(qdrant_path: str, kb_dir: str = "./candidate_kb") -> None:
    """
    Warn if any candidate_kb/*.md file is newer than the last kb_loader run.
    Logs a WARNING but does not block scoring.
    """
    ts_file = Path(qdrant_path) / ".kb_built_at"
    if not ts_file.exists():
        log.warning(
            "⚠️  找不到 KB 建立時間戳記 (%s)。\n"
            "   請執行 python -m utils.kb_loader 重建知識庫。",
            ts_file,
        )
        return

    kb_built_at = datetime.fromisoformat(ts_file.read_text(encoding="utf-8").strip())
    kb_built_at = kb_built_at.replace(tzinfo=timezone.utc)

    stale_files = []
    for md_file in Path(kb_dir).glob("*.md"):
        mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc)
        if mtime > kb_built_at:
            stale_files.append((md_file.name, mtime.strftime("%Y-%m-%dT%H:%M:%S")))

    if stale_files:
        names = ", ".join(f"{n} ({t})" for n, t in stale_files)
        log.warning(
            "⚠️  candidate_kb 有檔案比 Qdrant KB 新（KB 建於 %s）：\n"
            "   %s\n"
            "   建議執行 python -m utils.kb_loader 更新知識庫後再評分。",
            kb_built_at.strftime("%Y-%m-%dT%H:%M:%S"),
            names,
        )


def _qdrant_query(qdrant, vector: list[float], top_k: int) -> str:
    """Query Qdrant with a pre-computed vector and return formatted context."""
    result = qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=top_k,
    )
    parts = []
    for hit in result.points:
        source = hit.payload.get("source", "unknown")
        text = hit.payload.get("text", "")
        parts.append(f"[來源: {source}]\n{text}")
    return "\n---\n".join(parts)


def _batch_embed(texts: list[str], client, batch_size: int = 50) -> list[list[float]]:
    """Embed texts in batches, return one vector per text."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        resp = client.embeddings.create(model=emb_model(), input=batch)
        vectors.extend([e.embedding for e in resp.data])
        log.info("  embedded %d/%d JDs", min(start + batch_size, len(texts)), len(texts))
    return vectors


def retrieve_context(
    jd_text: str,
    qdrant_path: str,
    top_k: int = 5,
) -> str:
    """Embed a single JD text and retrieve relevant KB context. Used for single-job scoring."""
    from qdrant_client import QdrantClient

    client = make_client()
    qdrant = QdrantClient(path=qdrant_path)

    emb_resp = client.embeddings.create(
        model=emb_model(),
        input=jd_text[:8000],
    )
    return _qdrant_query(qdrant, emb_resp.data[0].embedding, top_k)


# ── Prompt Builder ─────────────────────────────────────────────────────────────

# Source bonus applied deterministically after LLM returns score (more reliable than prompt instruction)
SOURCE_BONUS: dict[str, int] = {
    "relocateme":   10,  # company actively offers relocation support
    "greenhouse":    5,  # direct ATS posting — company is actively hiring
    "lever":         5,  # direct ATS posting — company is actively hiring
    "bundesagentur": 5,  # official DE listing — higher visa-sponsorship rate
}


def build_prompt(
    jd_text: str,
    company: str,
    title: str,
    location: str,
    context: str,
    grading_rules: str,
) -> tuple[str, str]:
    system_prompt = f"""你是一位專業的求職顧問。請根據以下分級規則評估職缺與候選人的匹配度。

{grading_rules}

回覆必須是符合指定 JSON schema 的物件，不附加任何說明文字。"""

    user_prompt = f"""## 候選人背景
{context}

## 職缺資訊
公司：{company} | 職位：{title} | 地點：{location}

## 職缺描述
{jd_text}"""

    return system_prompt, user_prompt


# ── Main Scoring Loop ──────────────────────────────────────────────────────────


def score_jobs(
    db_path: str = DB_PATH,
    qdrant_path: str = QDRANT_PATH,
    rescore: bool = False,
    job_ids: list[str] | None = None,
) -> list[ScoringResult]:
    log.info("LLM provider: %s | model: %s", LLM_PROVIDER, chat_model())
    client = make_client()
    conn = init_db(db_path)

    _rules_path = Path(__file__).parent / "config" / "grading_rules.md"
    grading_rules = _rules_path.read_text(encoding="utf-8")

    if rescore:
        n = conn.execute(
            "UPDATE jobs SET status = 'un-scored' WHERE status = 'scored'"
        ).rowcount
        conn.commit()
        log.info("--rescore: reset %d already-scored jobs to un-scored", n)

    jobs = get_unscored_jobs(conn)
    if job_ids is not None:
        id_set = set(job_ids)
        jobs = [j for j in jobs if j["id"] in id_set]

    if not jobs:
        log.info("沒有待評分職缺")
        conn.close()
        return []

    # ── Pre-flight filters ────────────────────────────────────────────────────
    MIN_JD_CHARS = 100
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    expired_ids, valid_jobs = [], []
    short_jobs: list[tuple[str, int]] = []   # (job_id, jd_len)
    for job in jobs:
        exp = job.get("expires_at")
        if exp and exp < now_str:
            expired_ids.append(job["id"])
            log.info("expired: %s (%s @ %s, exp=%s)", job["id"], job["title"], job["company"], exp)
            continue
        jd_len = len(job.get("raw_jd_text") or "")
        if jd_len < MIN_JD_CHARS:
            short_jobs.append((job["id"], jd_len))
            log.info("short JD: %s (%s @ %s, %d chars)", job["id"], job["title"], job["company"], jd_len)
            continue
        valid_jobs.append(job)

    if expired_ids:
        n = mark_expired(conn, expired_ids)
        log.info("標記 %d 筆已過期職缺為 expired", n)
    for jid, jd_len in short_jobs:
        mark_error(conn, jid, f"JD too short ({jd_len} chars) — skipping LLM scoring")
    if short_jobs:
        log.info("標記 %d 筆 JD 過短職缺為 error", len(short_jobs))

    jobs = valid_jobs
    if not jobs:
        log.info("過濾後無待評分職缺")
        conn.close()
        return []

    log.info("開始評分 %d 筆職缺（已過濾 %d 過期、%d JD 過短）",
             len(jobs), len(expired_ids), len(short_jobs))

    kb_ready = check_kb_ready(qdrant_path)
    if kb_ready:
        check_kb_fresh(qdrant_path)
    if not kb_ready:
        log.error(
            "⚠️  候選人知識庫 (Qdrant) 尚未建立或為空！\n"
            "   評分將繼續，但所有職缺的候選人背景欄位都會是空的，\n"
            "   cover letter 和評分結果將缺乏個人化資訊。\n"
            "   請先執行：  python -m utils.kb_loader\n"
            "   確認 %s/candidate_kb 目錄存在且 candidate_kb/*.md 有內容。",
            qdrant_path,
        )

    # ── Batch-embed all JDs upfront (1 API call per 50 jobs vs 1 per job) ──────
    contexts: dict[str, str] = {}
    if kb_ready:
        try:
            from qdrant_client import QdrantClient
            qdrant = QdrantClient(path=qdrant_path)
            log.info("batch embedding %d JD(s) for RAG retrieval…", len(jobs))
            vectors = _batch_embed([j["raw_jd_text"][:8000] for j in jobs], client)
            for job, vec in zip(jobs, vectors):
                contexts[job["id"]] = _qdrant_query(qdrant, vec, top_k=5)
        except Exception as exc:
            log.warning("Batch RAG retrieval failed: %s — all jobs will use empty context", exc)

    scored: list[ScoringResult] = []

    for job in jobs:
        context = contexts.get(job["id"], "(候選人背景資料未載入)")

        system_prompt, user_prompt = build_prompt(
            jd_text=job["raw_jd_text"],
            company=job["company"],
            title=job["title"],
            location=job.get("location") or "",
            context=context,
            grading_rules=grading_rules,
        )

        # ── LLM call with retry ──
        result: ScoringResult | None = None
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = _call_llm(client, system_prompt, user_prompt)
                break
            except openai.RateLimitError as exc:
                last_exc = exc
                log.warning("RateLimitError on attempt %d/3 — sleeping 60s", attempt)
                time.sleep(60)
            except Exception as exc:
                last_exc = exc
                log.error(
                    "Scoring failed for job %s (%s @ %s): %s",
                    job["id"], job["title"], job["company"], exc,
                )
                break

        if result is None:
            mark_error(conn, job["id"], str(last_exc) if last_exc else "unknown error")
            log.warning("Marked job %s as error — will not retry automatically", job["id"])
            continue

        # ── Apply source bonus (deterministic, not LLM) ──
        bonus = SOURCE_BONUS.get(job.get("source", ""), 0)
        if bonus:
            original = result.match_score
            result.match_score = min(100, result.match_score + bonus)
            # Re-derive fit_grade after bonus (validator won't re-run on mutation)
            score = result.match_score
            lang  = result.jd_language_req
            result.fit_grade = (
                "C" if lang == "de_required" or score < 60
                else "A" if score >= 80
                else "B"
            )
            log.info(
                "source bonus +%d (%s): %d → %d | grade %s",
                bonus, job.get("source"), original, result.match_score, result.fit_grade,
            )

        # ── Persist ──
        update_score(
            conn,
            job["id"],
            {
                "match_score":      result.match_score,
                "fit_grade":        result.fit_grade,
                "top_3_reasons":    json.dumps(result.top_3_reasons, ensure_ascii=False),
                "cover_letter_draft": result.cover_letter_draft,
                "jd_language_req":  result.jd_language_req,
                "visa_restriction": result.visa_restriction,
                "salary_range":     result.salary_range,
                "contract_type":    result.contract_type,
                "scored_at":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )

        log.info(
            "[%s] %s @ %s | score=%d | lang=%s",
            result.fit_grade,
            job["title"],
            job["company"],
            result.match_score,
            result.jd_language_req,
        )
        scored.append(result)

    conn.close()
    return scored


def score_single_job(
    job_id: str,
    db_path: str = DB_PATH,
    qdrant_path: str = QDRANT_PATH,
) -> ScoringResult | None:
    """Reset one job to un-scored and immediately re-score it. Used by the dashboard."""
    conn = init_db(db_path)
    reset_to_unscored(conn, [job_id])
    conn.close()
    results = score_jobs(db_path=db_path, qdrant_path=qdrant_path, job_ids=[job_id])
    return results[0] if results else None


# ── Interview Brief Generator ──────────────────────────────────────────────────

BRIEF_PROMPT_TEMPLATE = """\
請根據以下職缺與候選人背景，生成結構化的面試準備單（繁體中文，Markdown 格式）。

## 職缺資訊
公司：{company} | 職位：{title} | 地點：{location}

## 職缺描述
{jd_text}

## 候選人背景
{context}

---

請依序輸出以下五個段落：

### 角色摘要
（2–3 句：這個職位在做什麼、所在團隊背景）

### 核心技術要求
（條列 JD 中的必要技術與技能）

### JD 暗示的挑戰與痛點
（公司想解決什麼問題、為什麼需要這個角色）

### 你的相關亮點
（從候選人背景中選出最相關的 2–3 項經歷，說明如何對應職缺需求）

### 可能被問到的問題
（根據此 JD 列出 5 個具體面試問題）\
"""


def generate_brief_for_job(
    job_id: str,
    db_path: str = DB_PATH,
    qdrant_path: str = QDRANT_PATH,
) -> str | None:
    """Generate and persist an interview preparation brief for one job.

    Called from the dashboard when a job transitions to interview_1.
    Returns the brief text, or None on failure.
    """
    conn = init_db(db_path)
    job = fetch_job_by_id(conn, job_id)
    if job is None:
        conn.close()
        log.warning("generate_brief_for_job: job %s not found", job_id)
        return None

    context = (
        retrieve_context(job["raw_jd_text"], qdrant_path)
        if check_kb_ready(qdrant_path)
        else "(候選人背景資料未載入 — 請先執行 utils.kb_loader)"
    )

    prompt = BRIEF_PROMPT_TEMPLATE.format(
        company=job["company"],
        title=job["title"],
        location=job.get("location") or "—",
        jd_text=(job["raw_jd_text"] or "")[:6000],
        context=context,
    )

    client = make_client()
    try:
        resp = client.chat.completions.create(
            model=chat_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        brief = resp.choices[0].message.content.strip()
        set_interview_brief(conn, job_id, brief)
        log.info("interview brief generated for job %s (%s @ %s)", job_id, job["title"], job["company"])
        conn.close()
        return brief
    except Exception as exc:
        log.error("Failed to generate interview brief for %s: %s", job_id, exc)
        conn.close()
        return None


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Phase 2 — Fit Scorer")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-score already-scored jobs (resets their status to un-scored first)",
    )
    args = parser.parse_args()
    scored = score_jobs(rescore=args.rescore)

    n = len(scored)
    log.info("完成評分 %d 筆", n)

    for grade in ["A", "B", "C"]:
        count = sum(1 for r in scored if r.fit_grade == grade)
        log.info("  %s 級：%d 筆", grade, count)

    en = sum(1 for r in scored if r.jd_language_req == "en_required")
    dp = sum(1 for r in scored if r.jd_language_req == "de_plus")
    dr = sum(1 for r in scored if r.jd_language_req == "de_required")
    log.info("  en_required: %d 筆 / de_plus: %d 筆 / de_required: %d 筆", en, dp, dr)
