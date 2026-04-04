"""
Phase 3 — Apply Assistant (Streamlit Dashboard)
Run: streamlit run phase3_dashboard.py
"""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pyperclip
import requests
import streamlit as st
import yaml

from utils.db import (
    init_db, upsert_job, update_status, set_follow_up, set_notes,
    add_interview_record, get_interview_records, delete_interview_record,
)

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(layout="wide", page_title="Job Dashboard")

# ── Helpers ────────────────────────────────────────────────────────────────────


def make_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def week_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")


@st.cache_resource
def get_conn():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    db_path = os.getenv("DB_PATH", "./data/jobs.db")
    return init_db(db_path)


@st.cache_data(ttl=30)
def load_config():
    with open("config/search_targets.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


PIPELINE_STATUSES = ('applied', 'interview_1', 'interview_2', 'offer', 'rejected')


def fetch_kpis(conn) -> dict:
    rows = conn.execute(f"""
        SELECT
            SUM(CASE WHEN fit_grade IN ('A','B') AND status='scored'         THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status IN {PIPELINE_STATUSES}                       THEN 1 ELSE 0 END) AS total_applied,
            SUM(CASE WHEN status IN {PIPELINE_STATUSES} AND applied_at >= ?   THEN 1 ELSE 0 END) AS week_applied,
            SUM(CASE WHEN status IN ('interview_1','interview_2','offer')      THEN 1 ELSE 0 END) AS in_interview,
            SUM(CASE WHEN status='offer'                                       THEN 1 ELSE 0 END) AS offers,
            SUM(CASE WHEN status IN {PIPELINE_STATUSES} AND follow_up_at <= ? AND follow_up_at IS NOT NULL
                                                                               THEN 1 ELSE 0 END) AS needs_followup,
            SUM(CASE WHEN status='error'                                        THEN 1 ELSE 0 END) AS errors
        FROM jobs
    """, (week_ago_iso(), today_iso())).fetchone()
    return {
        "pending":        rows["pending"]        or 0,
        "total_applied":  rows["total_applied"]  or 0,
        "week_applied":   rows["week_applied"]   or 0,
        "in_interview":   rows["in_interview"]   or 0,
        "offers":         rows["offers"]         or 0,
        "needs_followup": rows["needs_followup"] or 0,
        "errors":         rows["errors"]         or 0,
    }


def fetch_jobs(conn, grades, langs, sources, statuses) -> pd.DataFrame:
    if not (grades and langs and sources and statuses):
        return pd.DataFrame()

    def placeholders(lst):
        return ",".join("?" * len(lst))

    # error / un-scored jobs have no fit_grade — bypass that filter for them
    sql = f"""
        SELECT id, title, company, location,
               fit_grade, match_score, jd_language_req, source, status
        FROM jobs
        WHERE (fit_grade IN ({placeholders(grades)}) OR fit_grade IS NULL)
          AND (jd_language_req IN ({placeholders(langs)}) OR jd_language_req IS NULL)
          AND source           IN ({placeholders(sources)})
          AND status           IN ({placeholders(statuses)})
        ORDER BY
            CASE fit_grade WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 3 END,
            match_score DESC
    """
    params = grades + langs + sources + statuses
    rows = conn.execute(sql, params).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def fetch_job_detail(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


@st.cache_data(ttl=60)
def fetch_stats(_conn) -> dict:
    """Return all data needed for the stats expander."""
    # Grade distribution (scored + applied + skipped — jobs that went through scoring)
    grade_rows = _conn.execute("""
        SELECT fit_grade, COUNT(*) AS cnt
        FROM jobs
        WHERE status IN ('scored','applied','skipped') AND fit_grade IS NOT NULL
        GROUP BY fit_grade
    """).fetchall()
    grade_df = pd.DataFrame([dict(r) for r in grade_rows]).set_index("fit_grade") if grade_rows else pd.DataFrame()

    # Language breakdown
    lang_rows = _conn.execute("""
        SELECT COALESCE(jd_language_req, 'unknown') AS lang, COUNT(*) AS cnt
        FROM jobs
        WHERE status IN ('scored','applied','skipped')
        GROUP BY lang
    """).fetchall()
    lang_df = pd.DataFrame([dict(r) for r in lang_rows]).set_index("lang") if lang_rows else pd.DataFrame()

    # Source effectiveness
    src_rows = _conn.execute(f"""
        SELECT
            source,
            COUNT(*)                                                                        AS total,
            SUM(CASE WHEN fit_grade='A'                                  THEN 1 ELSE 0 END) AS grade_a,
            SUM(CASE WHEN fit_grade='B'                                  THEN 1 ELSE 0 END) AS grade_b,
            SUM(CASE WHEN status IN {PIPELINE_STATUSES}                  THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN status IN ('interview_1','interview_2','offer') THEN 1 ELSE 0 END) AS interviews
        FROM jobs
        WHERE status NOT IN ('un-scored','error')
        GROUP BY source
        ORDER BY grade_a DESC, total DESC
    """).fetchall()
    if src_rows:
        src_df = pd.DataFrame([dict(r) for r in src_rows])
        src_df["A 級率"] = src_df.apply(
            lambda r: f"{r['grade_a'] / r['total'] * 100:.0f}%" if r["total"] else "—", axis=1
        )
        src_df["回覆率"] = src_df.apply(
            lambda r: f"{r['interviews'] / r['applied'] * 100:.0f}%" if r["applied"] else "—", axis=1
        )
        src_df = src_df.rename(columns={
            "source": "來源", "total": "總筆數",
            "grade_a": "A 級", "grade_b": "B 級",
            "applied": "已投遞", "interviews": "進面試",
        })
    else:
        src_df = pd.DataFrame()

    # Application funnel
    f = _conn.execute(f"""
        SELECT
            SUM(CASE WHEN status IN {PIPELINE_STATUSES}                  THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN status IN ('interview_1','interview_2','offer') THEN 1 ELSE 0 END) AS interview_1,
            SUM(CASE WHEN status IN ('interview_2','offer')               THEN 1 ELSE 0 END) AS interview_2,
            SUM(CASE WHEN status='offer'                                  THEN 1 ELSE 0 END) AS offer
        FROM jobs
    """).fetchone()
    funnel_df = pd.DataFrame({
        "階段": ["已投遞", "一面邀約", "二面邀約", "Offer"],
        "筆數": [f["applied"] or 0, f["interview_1"] or 0,
                 f["interview_2"] or 0, f["offer"] or 0],
    }).set_index("階段")

    # Weekly application trend (last 8 weeks)
    week_rows = _conn.execute(f"""
        SELECT strftime('%Y-W%W', applied_at) AS week, COUNT(*) AS cnt
        FROM jobs
        WHERE status IN {PIPELINE_STATUSES} AND applied_at IS NOT NULL
        GROUP BY week
        ORDER BY week DESC
        LIMIT 8
    """).fetchall()
    week_df = pd.DataFrame([dict(r) for r in week_rows]).set_index("week").sort_index() if week_rows else pd.DataFrame()

    return {
        "grade_df":  grade_df,
        "lang_df":   lang_df,
        "src_df":    src_df,
        "funnel_df": funnel_df,
        "week_df":   week_df,
    }


GRADE_ICON = {"A": "🟢", "B": "🟡", "C": "🔴"}

# ── App ────────────────────────────────────────────────────────────────────────

conn = get_conn()
config = load_config()

# ── KPI row ────────────────────────────────────────────────────────────────────

kpis = fetch_kpis(conn)
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("待審閱 🎯",   kpis["pending"])
k2.metric("本週投遞 ✅", kpis["week_applied"])
k3.metric("面試中 📞",   kpis["in_interview"])
k4.metric("Offer 🎉",   kpis["offers"])
k5.metric("待跟進 🔔",   kpis["needs_followup"])
k6.metric("評分失敗 ❌", kpis["errors"])

with st.expander("📊 統計分析", expanded=False):
    stats = fetch_stats(conn)

    col_grade, col_lang = st.columns(2)

    with col_grade:
        st.markdown("**等級分布**")
        if not stats["grade_df"].empty:
            st.bar_chart(stats["grade_df"], y="cnt", x_label="等級", y_label="筆數")
        else:
            st.caption("尚無已評分職缺")

    with col_lang:
        st.markdown("**語言要求分布**")
        if not stats["lang_df"].empty:
            st.bar_chart(stats["lang_df"], y="cnt", x_label="語言要求", y_label="筆數")
        else:
            st.caption("尚無已評分職缺")

    st.markdown("**應聘漏斗**")
    if not stats["funnel_df"].empty and stats["funnel_df"]["筆數"].sum() > 0:
        st.bar_chart(stats["funnel_df"], y="筆數", x_label="階段", y_label="筆數")
    else:
        st.caption("尚無投遞記錄")

    st.markdown("**來源效益**")
    if not stats["src_df"].empty:
        st.dataframe(
            stats["src_df"][["來源", "總筆數", "A 級", "B 級", "已投遞", "進面試", "A 級率", "回覆率"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("尚無資料")

    st.markdown("**每週投遞趨勢（近 8 週）**")
    if not stats["week_df"].empty:
        st.bar_chart(stats["week_df"], y="cnt", x_label="週次", y_label="投遞數")
    else:
        st.caption("尚無投遞記錄")

with st.expander("🕐 Pipeline 排程記錄", expanded=False):
    log_path = Path("logs/pipeline.log")
    if log_path.exists():
        _log_text = log_path.read_text(encoding="utf-8", errors="replace")
        # Show last 100 lines
        _lines = _log_text.splitlines()
        st.code("\n".join(_lines[-100:]), language=None)
        st.caption(f"logs/pipeline.log — {log_path.stat().st_size / 1024:.1f} KB，顯示最後 100 行")
    else:
        st.info("尚無 pipeline 執行記錄。首次手動執行或等排程觸發後會出現。")

    _col1, _col2 = st.columns(2)
    with _col1:
        if st.button("▶️ 立即執行 Phase 1 + 2", use_container_width=True):
            import subprocess, os
            script = str(Path(__file__).parent / "run_pipeline.sh")
            st.info("啟動 pipeline（背景執行）…")
            subprocess.Popen(
                ["/bin/bash", script],
                cwd=str(Path(__file__).parent),
                stdout=open(log_path, "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            st.success("已啟動，稍後重新整理頁面查看結果。")
    with _col2:
        if st.button("🔄 重新整理記錄", use_container_width=True):
            st.rerun()

st.divider()

# ── Main columns ───────────────────────────────────────────────────────────────

left, right = st.columns([4, 6])

# ════════════════════════════════════════════════════════════════════════════════
# LEFT COLUMN
# ════════════════════════════════════════════════════════════════════════════════

with left:
    # ── Filters ──
    with st.expander("篩選條件", expanded=True):
        fit_grade_filter = st.multiselect(
            "等級", ["A", "B", "C"], default=["A", "B"]
        )
        lang_filter = st.multiselect(
            "語言要求",
            ["en_required", "de_plus", "de_required", "unknown"],
            default=["en_required", "de_plus", "unknown"],
        )
        source_filter = st.multiselect(
            "來源",
            ["arbeitnow", "englishjobs", "remotive", "jobicy",
             "relocateme", "bundesagentur", "greenhouse", "lever",
             "linkedin", "stepstone", "other"],
            default=["arbeitnow", "englishjobs", "remotive", "jobicy",
                     "relocateme", "bundesagentur", "greenhouse", "lever",
                     "linkedin", "stepstone", "other"],
        )
        status_filter = st.multiselect(
            "狀態",
            ["un-scored", "scored", "applied", "interview_1", "interview_2",
             "offer", "rejected", "skipped", "error", "expired"],
            default=["scored"],
        )

    # ── Job table ──
    df = fetch_jobs(conn, fit_grade_filter, lang_filter, source_filter, status_filter)

    if df.empty:
        st.info("沒有符合條件的職缺。")
        selected_job_id = None
    else:
        event = st.dataframe(
            df[["title", "company", "location", "fit_grade",
                "match_score", "jd_language_req", "source", "status"]],
            use_container_width=True,
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="job_table",
        )

        selected_rows = event.selection.get("rows", []) if event.selection else []
        if selected_rows:
            st.session_state["selected_idx"] = selected_rows[0]

        selected_job_id = (
            df.iloc[st.session_state["selected_idx"]]["id"]
            if "selected_idx" in st.session_state
            and st.session_state["selected_idx"] < len(df)
            else None
        )

    # ── LinkedIn search buttons ──
    st.markdown("**LinkedIn 搜尋**")
    linkedin_urls = config.get("linkedin_search_urls", [])
    if linkedin_urls:
        link_cols = st.columns(len(linkedin_urls))
        for i, url in enumerate(linkedin_urls):
            label = url.split("keywords=")[1].split("&")[0].replace("+", " ")
            with link_cols[i]:
                st.link_button(f"🔍 {label}", url, use_container_width=True)

    # ── Search targets manager ──
    with st.expander("⚙️ 管理搜尋條件", expanded=False):
        cfg = load_config()  # cached read

        tab_kw, tab_gh, tab_lv = st.tabs(["關鍵字", "Greenhouse", "Lever"])

        def _lines(lst: list) -> str:
            return "\n".join(str(x) for x in (lst or []))

        def _parse_lines(text: str) -> list[str]:
            return [ln.strip() for ln in text.splitlines() if ln.strip()]

        with tab_kw:
            st.caption("Arbeitnow 與 Bundesagentur 共用這份關鍵字清單（一行一個）")
            an_kw = st.text_area(
                "Arbeitnow 關鍵字",
                value=_lines(cfg.get("arbeitnow", {}).get("keywords", [])),
                height=200,
                key="mgr_an_kw",
                label_visibility="collapsed",
            )
            ba_kw = st.text_area(
                "Bundesagentur 關鍵字",
                value=_lines(cfg.get("bundesagentur", {}).get("keywords", [])),
                height=160,
                key="mgr_ba_kw",
            )
            if st.button("💾 儲存關鍵字", key="save_kw", use_container_width=True):
                cfg["arbeitnow"]["keywords"]    = _parse_lines(an_kw)
                cfg["bundesagentur"]["keywords"] = _parse_lines(ba_kw)
                with open("config/search_targets.yaml", "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
                load_config.clear()
                st.success("✅ 關鍵字已儲存")

        with tab_gh:
            st.caption("一行一個 slug（不需加引號）。404 的 slug 執行時自動跳過。")
            gh_slugs = st.text_area(
                "Greenhouse 公司 slug",
                value=_lines(cfg.get("greenhouse", {}).get("companies", [])),
                height=280,
                key="mgr_gh",
                label_visibility="collapsed",
            )
            if st.button("💾 儲存 Greenhouse 清單", key="save_gh", use_container_width=True):
                if "greenhouse" not in cfg:
                    cfg["greenhouse"] = {}
                cfg["greenhouse"]["companies"] = _parse_lines(gh_slugs)
                with open("config/search_targets.yaml", "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
                load_config.clear()
                st.success("✅ Greenhouse 清單已儲存")

        with tab_lv:
            st.caption("一行一個 slug（不需加引號）。404 的 slug 執行時自動跳過。")
            lv_slugs = st.text_area(
                "Lever 公司 slug",
                value=_lines(cfg.get("lever", {}).get("companies", [])),
                height=280,
                key="mgr_lv",
                label_visibility="collapsed",
            )
            if st.button("💾 儲存 Lever 清單", key="save_lv", use_container_width=True):
                if "lever" not in cfg:
                    cfg["lever"] = {}
                cfg["lever"]["companies"] = _parse_lines(lv_slugs)
                with open("config/search_targets.yaml", "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
                load_config.clear()
                st.success("✅ Lever 清單已儲存")

    # ── Manual add ──
    with st.expander("手動新增職缺", expanded=False):
        with st.form("manual_add"):
            company  = st.text_input("公司名稱 *")
            title    = st.text_input("職位名稱 *")
            url      = st.text_input("職缺 URL *")
            location = st.text_input("地點")
            source   = st.selectbox("來源", ["linkedin", "stepstone", "bundesagentur", "other"])
            raw_jd   = st.text_area("職缺描述（貼入 JD 全文）*", height=200)
            submit   = st.form_submit_button("加入資料庫")

        if submit:
            if not all([company, title, url, raw_jd]):
                st.error("請填入所有必填欄位（標 * 者）")
            else:
                job = {
                    "id":          make_id(url),
                    "company":     company,
                    "title":       title,
                    "url":         url,
                    "source":      source,
                    "source_tier": "manual",
                    "location":    location,
                    "raw_jd_text": raw_jd,
                    "fetched_at":  utcnow_iso(),
                    "status":      "un-scored",
                }
                added = upsert_job(conn, job)
                if added:
                    st.success("✅ 已加入資料庫。請執行 `python phase2_scorer.py` 後重新整理頁面。")
                else:
                    st.warning("⚠️ 此 URL 已存在於資料庫中。")

# ════════════════════════════════════════════════════════════════════════════════
# RIGHT COLUMN
# ════════════════════════════════════════════════════════════════════════════════

with right:
    if "selected_idx" not in st.session_state or not selected_job_id:
        st.info("← 從左側選擇一筆職缺")
    else:
        job = fetch_job_detail(conn, selected_job_id)
        if job is None:
            st.warning("找不到該職缺，請重新整理頁面。")
        else:
            grade = job.get("fit_grade") or "C"
            st.subheader(f"{GRADE_ICON.get(grade, '')} {job['title']}")
            st.caption(f"{job['company']} · {job.get('location') or '—'} · {job['source']}")

            # ── Visa warning ──────────────────────────────────────────────
            visa = job.get("visa_restriction") or "unclear"
            if visa == "eu_only":
                st.error("🚫 **EU Only / 需自備工作許可** — 投遞前請先確認 Chancenkarte 是否符合條件")
            elif visa == "sponsored":
                st.success("✅ **公司提供簽證協助** — Chancenkarte 友善")

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Match Score",   job.get("match_score") or "—")
            c2.metric("Fit Grade",     grade)
            c3.metric("Language Req",  job.get("jd_language_req") or "—")
            VISA_LABEL = {"open": "🟢 open", "eu_only": "🔴 EU only",
                          "sponsored": "🟢 sponsored", "unclear": "⚪ unclear"}
            c4.metric("Visa",          VISA_LABEL.get(visa, visa))
            CONTRACT_LABEL = {"permanent": "✅ 正職", "contract": "⏳ 合約",
                              "freelance": "🔧 Freelance", "unknown": "— unknown"}
            c5.metric("Contract",      CONTRACT_LABEL.get(job.get("contract_type") or "unknown", "—"))

            if job.get("salary_range"):
                st.caption(f"💰 薪資：{job['salary_range']}")

            # ── Company Research ──────────────────────────────────────────────
            _research = job.get("company_research")
            with st.expander("🔍 公司研究" + ("  ✓" if _research else ""), expanded=False):
                if _research:
                    st.markdown(_research)
                else:
                    st.caption("尚未生成公司研究報告。")
                _r_col1, _r_col2 = st.columns(2)
                with _r_col1:
                    if st.button(
                        "🔍 生成公司研究" if not _research else "🔄 重新生成",
                        key=f"research_{job['id']}",
                        use_container_width=True,
                        type="primary" if not _research else "secondary",
                    ):
                        import os
                        from dotenv import load_dotenv
                        from utils.company_researcher import research_company
                        load_dotenv()
                        with st.spinner(f"正在研究 {job['company']}…"):
                            research_company(
                                job["id"],
                                db_path=os.getenv("DB_PATH", "./data/jobs.db"),
                            )
                        st.cache_data.clear()
                        st.rerun()
                with _r_col2:
                    st.link_button(
                        "🌐 Kununu 評價",
                        f"https://www.kununu.com/de/search?term={requests.utils.quote(job['company'])}",
                        use_container_width=True,
                    )

            # Top 3 reasons
            st.markdown("**📌 評分理由**")
            reasons_raw = job.get("top_3_reasons")
            reasons = []
            if reasons_raw:
                try:
                    reasons = json.loads(reasons_raw)
                    if not isinstance(reasons, list):
                        reasons = [str(reasons)]
                except json.JSONDecodeError:
                    reasons = [reasons_raw]
            if reasons:
                for r in reasons:
                    st.markdown(f"- {r}")
            else:
                st.caption("（尚未評分）")

            st.divider()

            # Cover letter editor
            st.markdown("**✉️ Cover Letter（可直接編輯）**")
            edited_cl = st.text_area(
                label="cover_letter",
                value=job.get("cover_letter_draft") or "",
                height=280,
                label_visibility="collapsed",
                key=f"cl_{job['id']}",
            )
            word_count = len(edited_cl.split()) if edited_cl.strip() else 0
            st.caption(f"字數：{word_count} / 建議 200–400 字")
            if word_count > 400:
                st.warning("⚠️ Cover Letter 超過 400 字，建議壓縮後再複製")

            # Tone regeneration
            _tone_col, _btn_col = st.columns([3, 2])
            with _tone_col:
                _tone = st.selectbox(
                    "語氣",
                    options=["formal", "startup", "concise"],
                    format_func=lambda x: {
                        "formal":  "🏛️ Formal — 正式企業風格",
                        "startup": "🚀 Startup — 直接有個性",
                        "concise": "✂️ Concise — 200 字以內，精準",
                    }[x],
                    key=f"tone_{job['id']}",
                    label_visibility="collapsed",
                )
            with _btn_col:
                if st.button("🔄 重新生成 Cover Letter", use_container_width=True, key=f"regen_cl_{job['id']}"):
                    import os
                    from dotenv import load_dotenv
                    from phase2_scorer import regenerate_cover_letter
                    load_dotenv()
                    with st.spinner(f"以 {_tone} 語氣重新生成中…"):
                        regenerate_cover_letter(
                            job["id"],
                            tone=_tone,
                            db_path=os.getenv("DB_PATH", "./data/jobs.db"),
                            qdrant_path=os.getenv("QDRANT_PATH", "./qdrant_data"),
                        )
                    st.cache_data.clear()
                    st.rerun()

            if edited_cl.strip():
                import io
                from docx import Document as DocxDocument
                _doc = DocxDocument()
                _doc.add_heading(f"{job['title']} @ {job['company']}", level=1)
                for para in edited_cl.strip().split("\n"):
                    _doc.add_paragraph(para)
                _buf = io.BytesIO()
                _doc.save(_buf)
                st.download_button(
                    label="📄 下載 .docx",
                    data=_buf.getvalue(),
                    file_name=f"cover_letter_{job['company']}_{job['title']}.docx".replace(" ", "_"),
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    key=f"dl_{job['id']}",
                )

            st.divider()

            # ── Action buttons (status-conditional) ──────────────────────────
            cur_status = job.get("status", "")

            def _transition(new_status: str) -> None:
                update_status(conn, job["id"], new_status)
                st.session_state.pop("selected_idx", None)
                st.cache_data.clear()
                st.rerun()

            if cur_status in ("scored", "un-scored", "error"):
                btn_cols = st.columns(5)
                with btn_cols[0]:
                    st.link_button("🌐 開啟職缺頁面", job["url"], use_container_width=True)
                with btn_cols[1]:
                    if st.button("📋 複製 Cover Letter", use_container_width=True, key=f"copy_{job['id']}"):
                        try:
                            pyperclip.copy(edited_cl)
                            st.success("已複製 ✓")
                        except Exception:
                            st.info("Docker 環境無剪貼簿，請手動選取 Cover Letter 文字後複製。")
                with btn_cols[2]:
                    if st.button("✅ 已投遞", use_container_width=True, type="primary", key=f"apply_{job['id']}"):
                        update_status(conn, job["id"], "applied", applied_at=utcnow_iso())
                        set_follow_up(conn, job["id"],
                            (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d"))
                        st.session_state.pop("selected_idx", None)
                        st.cache_data.clear()
                        st.rerun()
                with btn_cols[3]:
                    if st.button("⏭️ 略過", use_container_width=True, key=f"skip_{job['id']}"):
                        _transition("skipped")
                with btn_cols[4]:
                    if st.button("🔄 重新評分", use_container_width=True, key=f"rescore_{job['id']}"):
                        import os
                        from dotenv import load_dotenv
                        from phase2_scorer import score_single_job
                        load_dotenv()
                        with st.spinner("評分中…"):
                            score_single_job(job["id"],
                                db_path=os.getenv("DB_PATH", "./data/jobs.db"),
                                qdrant_path=os.getenv("QDRANT_PATH", "./qdrant_data"))
                        st.cache_data.clear()
                        st.rerun()

            elif cur_status == "applied":
                btn_cols = st.columns(3)
                with btn_cols[0]:
                    st.link_button("🌐 開啟職缺頁面", job["url"], use_container_width=True)
                with btn_cols[1]:
                    if st.button("📞 面試邀約", use_container_width=True, type="primary", key=f"iv1_{job['id']}"):
                        import os
                        from dotenv import load_dotenv
                        from phase2_scorer import generate_brief_for_job
                        load_dotenv()
                        with st.spinner("正在生成面試準備單…"):
                            generate_brief_for_job(
                                job["id"],
                                db_path=os.getenv("DB_PATH", "./data/jobs.db"),
                                qdrant_path=os.getenv("QDRANT_PATH", "./qdrant_data"),
                            )
                        _transition("interview_1")
                with btn_cols[2]:
                    if st.button("❌ 已拒絕", use_container_width=True, key=f"rej_{job['id']}"):
                        _transition("rejected")

            elif cur_status == "interview_1":
                btn_cols = st.columns(3)
                with btn_cols[0]:
                    st.link_button("🌐 開啟職缺頁面", job["url"], use_container_width=True)
                with btn_cols[1]:
                    if st.button("💻 二次面試", use_container_width=True, type="primary", key=f"iv2_{job['id']}"):
                        _transition("interview_2")
                with btn_cols[2]:
                    if st.button("❌ 已拒絕", use_container_width=True, key=f"rej_{job['id']}"):
                        _transition("rejected")

            elif cur_status == "interview_2":
                btn_cols = st.columns(3)
                with btn_cols[0]:
                    st.link_button("🌐 開啟職缺頁面", job["url"], use_container_width=True)
                with btn_cols[1]:
                    if st.button("🎉 收到 Offer", use_container_width=True, type="primary", key=f"offer_{job['id']}"):
                        _transition("offer")
                with btn_cols[2]:
                    if st.button("❌ 已拒絕", use_container_width=True, key=f"rej_{job['id']}"):
                        _transition("rejected")

            else:  # offer / rejected / skipped / expired — terminal, open only
                if cur_status == "expired":
                    st.warning("⏰ 此職缺已超過有效期限（expires_at 已過）")
                st.link_button("🌐 開啟職缺頁面", job["url"])

            # ── Interview Brief ──────────────────────────────────────────────
            if cur_status in ("interview_1", "interview_2", "offer"):
                st.divider()
                brief_header, brief_btn = st.columns([5, 1])
                with brief_header:
                    st.markdown("**🎯 面試準備單**")
                with brief_btn:
                    if st.button("🔄 重新生成", key=f"regen_brief_{job['id']}", use_container_width=True):
                        import os
                        from dotenv import load_dotenv
                        from phase2_scorer import generate_brief_for_job
                        load_dotenv()
                        with st.spinner("生成中…"):
                            generate_brief_for_job(
                                job["id"],
                                db_path=os.getenv("DB_PATH", "./data/jobs.db"),
                                qdrant_path=os.getenv("QDRANT_PATH", "./qdrant_data"),
                            )
                        st.cache_data.clear()
                        st.rerun()

                brief = job.get("interview_brief")
                if brief:
                    st.markdown(brief)
                    st.download_button(
                        label="⬇️ 下載 Markdown",
                        data=f"# {job['title']} @ {job['company']}\n\n{brief}\n",
                        file_name=f"interview_brief_{job['company']}_{job['title']}.md".replace(" ", "_"),
                        mime="text/markdown",
                        key=f"dl_brief_{job['id']}",
                    )
                else:
                    st.caption("（尚未生成面試準備單 — 點「🔄 重新生成」手動產生）")

            # ── Interview Records ────────────────────────────────────────────
            if cur_status in ("interview_1", "interview_2", "offer", "rejected"):
                st.divider()
                st.markdown("**📋 面試記錄**")

                records = get_interview_records(conn, job["id"])

                # Display existing records
                if records:
                    ROUND_LABEL = {"interview_1": "第一輪", "interview_2": "第二輪", "other": "其他"}
                    FORMAT_LABEL = {"phone": "☎️ 電話", "video": "💻 視訊", "onsite": "🏢 現場", "technical": "⌨️ 技術面試"}
                    STAR = ["", "★☆☆☆☆", "★★☆☆☆", "★★★☆☆", "★★★★☆", "★★★★★"]
                    for rec in records:
                        label = ROUND_LABEL.get(rec["round"], rec["round"])
                        date_str = rec.get("interview_date") or "—"
                        with st.expander(f"{label}　{date_str}　{FORMAT_LABEL.get(rec.get('format',''), rec.get('format',''))}　{STAR[rec.get('self_rating') or 0]}", expanded=False):
                            if rec.get("interviewer"):
                                st.caption(f"面試官：{rec['interviewer']}")
                            if rec.get("questions"):
                                st.markdown("**被問到的問題**")
                                st.markdown(rec["questions"])
                            if rec.get("impressions"):
                                st.markdown("**感想**")
                                st.markdown(rec["impressions"])
                            if st.button("🗑️ 刪除此記錄", key=f"del_rec_{rec['id']}", type="secondary"):
                                delete_interview_record(conn, rec["id"])
                                st.cache_data.clear()
                                st.rerun()

                # Add new record form
                with st.expander("➕ 新增面試記錄", expanded=not records):
                    with st.form(key=f"interview_record_form_{job['id']}"):
                        _default_round = cur_status if cur_status in ("interview_1", "interview_2") else "other"
                        _round_options = ["interview_1", "interview_2", "other"]
                        _round_labels  = ["第一輪", "第二輪", "其他"]
                        ir_round = st.selectbox(
                            "輪次",
                            options=_round_options,
                            format_func=lambda x: _round_labels[_round_options.index(x)],
                            index=_round_options.index(_default_round),
                        )
                        ir_col1, ir_col2 = st.columns(2)
                        with ir_col1:
                            ir_date = st.date_input("面試日期", value=datetime.now(timezone.utc).date())
                        with ir_col2:
                            ir_format = st.selectbox(
                                "形式",
                                options=["video", "phone", "onsite", "technical"],
                                format_func=lambda x: {"video": "💻 視訊", "phone": "☎️ 電話", "onsite": "🏢 現場", "technical": "⌨️ 技術面試"}[x],
                            )
                        ir_interviewer = st.text_input("面試官（姓名 / 職稱）", placeholder="e.g. Sarah, Engineering Manager")
                        ir_questions = st.text_area("被問到的問題", height=120, placeholder="- Tell me about yourself\n- How do you handle …")
                        ir_rating = st.slider("自我感覺", min_value=1, max_value=5, value=3, help="1 = 很差，5 = 很好")
                        ir_impressions = st.text_area("感想 / 注意事項", height=80, placeholder="公司文化感受、下一步注意事項…")
                        if st.form_submit_button("💾 儲存記錄", use_container_width=True, type="primary"):
                            add_interview_record(conn, {
                                "job_id":         job["id"],
                                "round":          ir_round,
                                "interview_date": ir_date.isoformat(),
                                "interviewer":    ir_interviewer or None,
                                "format":         ir_format,
                                "questions":      ir_questions or None,
                                "self_rating":    ir_rating,
                                "impressions":    ir_impressions or None,
                                "created_at":     utcnow_iso(),
                            })
                            st.cache_data.clear()
                            st.rerun()

            # ── Notes ────────────────────────────────────────────────────────
            st.divider()
            st.markdown("**📝 備註**")
            note_val = st.text_area(
                label="notes",
                value=job.get("notes") or "",
                height=80,
                placeholder="面試感想、聯絡人、薪資範圍…",
                label_visibility="collapsed",
                key=f"notes_{job['id']}",
            )
            if st.button("儲存備註", key=f"save_notes_{job['id']}"):
                set_notes(conn, job["id"], note_val)
                st.cache_data.clear()
                st.success("備註已儲存 ✓")

            # ── Follow-up section (all active pipeline stages) ───────────────
            if cur_status in PIPELINE_STATUSES:
                st.divider()
                st.markdown("**🔔 跟進提醒**")

                current_date = job.get("follow_up_at")
                default_date = (
                    datetime.fromisoformat(current_date).date()
                    if current_date
                    else (datetime.now(timezone.utc) + timedelta(days=7)).date()
                )

                fu_col1, fu_col2 = st.columns([3, 1])
                with fu_col1:
                    new_date = st.date_input(
                        "跟進日期",
                        value=default_date,
                        key=f"fu_date_{job['id']}",
                        label_visibility="collapsed",
                    )
                with fu_col2:
                    if st.button("儲存", key=f"fu_save_{job['id']}", use_container_width=True):
                        set_follow_up(conn, job["id"], new_date.isoformat())
                        st.cache_data.clear()
                        st.success(f"已設定跟進日期：{new_date.isoformat()}")

                if current_date:
                    today = today_iso()
                    if current_date <= today:
                        st.warning(f"⚠️ 跟進日期已到（{current_date}）— 記得發跟進信！")
                    else:
                        days_left = (
                            datetime.fromisoformat(current_date).date()
                            - datetime.now(timezone.utc).date()
                        ).days
                        st.caption(f"距跟進日期還有 {days_left} 天（{current_date}）")
