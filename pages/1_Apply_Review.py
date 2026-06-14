"""Apply review page (Step 5.2) — the Tier 2 approval queue.

Streamlit multipage: lives in pages/ next to phase3_dashboard.py and shows
up in the sidebar nav automatically. All snapshot reads/writes go through
utils.snapshot_io so the lifecycle rules hold no matter where a decision
is made.

Approving writes approved_at; apply_session.py (Step 5.3) picks approved
snapshots up on the host. Tier 3 drafts render as a copy-paste answer
sheet (st.code = built-in copy button) instead of approval buttons.
"""

import os

import streamlit as st
from dotenv import load_dotenv

from utils.db import init_db
from utils.snapshot_io import (
    abandon_snapshot,
    approve_snapshot,
    fetch_work,
    last_failure,
)

st.set_page_config(layout="wide", page_title="Apply Review")

_STRINGS = {
    "zh": {
        "title": "投遞審閱佇列",
        "intro": "Stage 1 草稿在此審閱。批准後由 host 端 apply_session 取走執行;"
                 "Tier 3 用小抄自行貼上。",
        "drafts": "待審草稿", "approved": "已批准待送出", "submitted": "已送出",
        "failed_n": "執行失敗", "empty": "目前沒有待審草稿。先跑 apply_stage1.py。",
        "tier_filter": "Tier 篩選", "all": "全部",
        "match": "匹配", "created": "建立於", "channel": "管道",
        "last_fail": "上次執行失敗", "verifier_pass": "verifier 通過",
        "verifier_issues": "verifier 發現的問題", "verifier_skip": "verifier 未跑(無生成內容)",
        "verifier_blocking": "需處理的阻擋問題", "verifier_minor": "次要提醒",
        "friction_easy": "易投", "friction_medium": "需審", "friction_painful": "填好待按",
        "friction_account": "需手動/帳號",
        "tab_actions": "填值表", "tab_cl": "Cover Letter", "tab_qa": "自訂問答",
        "tab_sheet": "小抄(點右上複製)",
        "cl_flagged": "這封 cover letter 被標記的疑慮(送出前請確認)",
        "unfilled": "未填欄位", "never_fill": "拒填欄位(never_fill)",
        "no_actions": "無自動填值(Tier 3 頁面)", "no_cl": "本表單沒有 cover letter 欄位",
        "no_qa": "沒有自訂問題", "notes": "備註",
        "approve": "批准", "abandon": "放棄", "abandon_reason": "放棄原因(選填)",
        "approved_hint": "在 host 端執行:python apply_session.py(預設 prepare 模式)",
        "cancel_approval": "撤回(標 abandoned,職缺會回佇列重生成)",
        "required_mark": "必填",
    },
    "en": {
        "title": "Apply Review Queue",
        "intro": "Review Stage 1 drafts here. Approved snapshots are picked up by "
                 "apply_session on the host; Tier 3 uses the answer sheet.",
        "drafts": "Drafts", "approved": "Approved, awaiting session", "submitted": "Submitted",
        "failed_n": "Failed runs", "empty": "No drafts to review. Run apply_stage1.py first.",
        "tier_filter": "Tier filter", "all": "All",
        "match": "Match", "created": "Created", "channel": "Channel",
        "last_fail": "Last execution failure", "verifier_pass": "verifier passed",
        "verifier_issues": "Verifier issues", "verifier_skip": "verifier skipped (nothing generated)",
        "verifier_blocking": "Blocking issues", "verifier_minor": "Minor notes",
        "friction_easy": "easy", "friction_medium": "review", "friction_painful": "fill+press",
        "friction_account": "manual/account",
        "tab_actions": "Fill plan", "tab_cl": "Cover Letter", "tab_qa": "Custom Q&A",
        "tab_sheet": "Answer sheet (copy top-right)",
        "cl_flagged": "Flags on this cover letter (confirm before submitting)",
        "unfilled": "Unfilled fields", "never_fill": "Refused fields (never_fill)",
        "no_actions": "No automatic fills (Tier 3 page)", "no_cl": "No cover letter slot on this form",
        "no_qa": "No custom questions", "notes": "Notes",
        "approve": "Approve", "abandon": "Abandon", "abandon_reason": "Reason (optional)",
        "approved_hint": "Run on the host: python apply_session.py (prepare mode by default)",
        "cancel_approval": "Withdraw (marks abandoned; the job re-queues)",
        "required_mark": "required",
    },
}


def T(key: str) -> str:  # noqa: N802 — same convention as phase3_dashboard
    lang = st.session_state.get("lang", "zh")
    return _STRINGS[lang].get(key, _STRINGS["en"].get(key, key))


def get_conn():
    load_dotenv()
    return init_db(os.getenv("DB_PATH", "./data/jobs.db"))


def _count(conn, status: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM application_snapshots WHERE status = ?", (status,)
    ).fetchone()[0]


def _issue_line(issue: dict) -> str:
    return f"`{issue.get('where', '')}` — {issue.get('issue', '')}"


def _cl_flags(report: dict | None) -> list[dict]:
    """Verifier issues that bear on the cover letter — fabrication anywhere, or
    anything the reviewer pinned to the cover_letter slot. Surfaced next to the
    letter itself so a flagged claim is read in context, not buried in the
    generic verifier block."""
    if not isinstance(report, dict):
        return []
    # high-severity only — keep the alarm-fatigue fix (watchlist #13): a low
    # cosmetic note about the letter stays in the muted verifier expander, it
    # does not turn the Cover Letter tab red. fabrication is always high (B).
    return [i for i in (report.get("issues") or [])
            if i.get("severity") == "high"
            and (i.get("kind") == "fabrication" or i.get("where") == "cover_letter")]


def _verifier_block(report: dict | None) -> None:
    """Triage display (watchlist #13): blocking issues stay loud and red,
    minor notes collapse into a muted expander so a draft with only low-
    severity nits doesn't read like a failure (alarm-fatigue fix)."""
    if not isinstance(report, dict) or not report:
        st.caption(T("verifier_skip"))
        return
    issues = report.get("issues") or []
    if report.get("pass") and not issues:
        st.success(T("verifier_pass"))
        return
    highs = [i for i in issues if i.get("severity") == "high"]
    lows = [i for i in issues if i.get("severity") != "high"]
    if highs:
        st.error(f"⛔ {T('verifier_blocking')} ({len(highs)})")
        for issue in highs:
            st.error(_issue_line(issue))
    if lows:
        with st.expander(f"ℹ️ {T('verifier_minor')} ({len(lows)})", expanded=False):
            for issue in lows:
                st.caption(_issue_line(issue))


# friction = how much manual work the human still owns after Stage 1.
# Lower rank floats to the top of the review queue (high-value, low-effort
# first). Tier is the coarse signal; channel/payload refine Tier 3.
def _friction(snap: dict) -> tuple[int, str]:
    tier = snap.get("tier") or 9
    if tier == 1:
        return 0, T("friction_easy")
    if tier == 2:
        return 1, T("friction_medium")
    channel = (snap.get("channel") or "").lower()
    manual = ("board" in channel or "indeed" in channel or "stepstone" in channel
              or "account" in channel or "no-form" in channel)
    has_fills = bool((snap.get("form_payload") or {}).get("actions"))
    if not manual and has_fills:
        return 2, T("friction_painful")    # captcha: filled, human just presses
    return 3, T("friction_account")        # board / account wall: fully manual


def _actions_tab(payload: dict) -> None:
    actions = payload.get("actions") or []
    if actions:
        st.dataframe(
            [{"label": a.get("label"), "action": a.get("action"),
              "value": a.get("value"), "source": a.get("source"),
              "review": "⚠️" if a.get("needs_review") else ""}
             for a in actions],
            width="stretch", hide_index=True)
    else:
        st.caption(T("no_actions"))
    unfilled = payload.get("unfilled") or []
    if unfilled:
        st.markdown(f"**{T('unfilled')}**")
        for u in unfilled:
            req = f" ({T('required_mark')})" if u.get("required") else ""
            st.caption(f"• {u.get('label') or u.get('selector')}{req} — {u.get('reason')}")
    skipped = payload.get("never_fill_skipped") or []
    if skipped:
        st.caption(f"{T('never_fill')}: {', '.join(skipped)}")


def _sheet_tab(snap: dict, payload: dict) -> None:
    """Every value as its own st.code block — one click per copy."""
    for a in payload.get("actions") or []:
        if a.get("value"):
            st.caption(a.get("label") or a.get("selector"))
            st.code(a["value"], language=None)
    for key, value in (payload.get("answer_sheet") or {}).items():
        st.caption(key)
        st.code(value, language=None)
    for qa in snap.get("custom_qa") or []:
        st.caption(qa.get("question", ""))
        st.code(qa.get("answer", ""), language=None)
    if snap.get("cover_letter"):
        st.caption("cover letter")
        st.code(snap["cover_letter"], language=None)


def _draft_card(conn, snap: dict) -> None:
    job = snap["job"]
    payload = snap.get("form_payload") or {}
    tier = snap.get("tier")
    score = job.get("match_score")
    _, friction_label = _friction(snap)
    header = (f"T{tier} [{friction_label}] · {job.get('company')} — {job.get('title')}"
              + (f" · {T('match')} {score}" if score is not None else ""))
    with st.expander(header, expanded=False):
        st.caption(f"{T('channel')}: {snap.get('channel')} · "
                   f"{T('created')}: {snap.get('created_at')} · "
                   f"[{snap.get('apply_url')}]({snap.get('apply_url')})")

        fail = last_failure(conn, snap["job_id"])
        if fail:
            st.error(f"{T('last_fail')} ({fail['created_at']}):\n\n{fail.get('notes')}")

        _verifier_block(snap.get("verifier_report"))

        tab_a, tab_cl, tab_qa, tab_sheet = st.tabs(
            [T("tab_actions"), T("tab_cl"), T("tab_qa"), T("tab_sheet")])
        with tab_a:
            _actions_tab(payload)
        with tab_cl:
            if snap.get("cover_letter"):
                flags = _cl_flags(snap.get("verifier_report"))
                if flags:
                    st.error(f"⚠️ {T('cl_flagged')}")
                    for issue in flags:
                        st.error(_issue_line(issue))
                st.code(snap["cover_letter"], language=None)
            else:
                st.caption(T("no_cl"))
        with tab_qa:
            for qa in snap.get("custom_qa") or []:
                st.markdown(f"**{qa.get('question', '')}**")
                st.code(qa.get("answer", ""), language=None)
            if not snap.get("custom_qa"):
                st.caption(T("no_qa"))
        with tab_sheet:
            _sheet_tab(snap, payload)

        if snap.get("notes"):
            st.caption(f"{T('notes')}: {snap['notes']}")

        cols = st.columns([1, 1, 3])
        if tier != 3 and cols[0].button(
                T("approve"), key=f"approve_{snap['id']}", type="primary"):
            approve_snapshot(conn, snap["id"])
            st.rerun()
        reason = cols[2].text_input(
            T("abandon_reason"), key=f"reason_{snap['id']}",
            label_visibility="collapsed", placeholder=T("abandon_reason"))
        if cols[1].button(T("abandon"), key=f"abandon_{snap['id']}"):
            abandon_snapshot(conn, snap["id"], reason)
            st.rerun()


conn = get_conn()
st.title(T("title"))
st.caption(T("intro"))

m1, m2, m3, m4 = st.columns(4)
m1.metric(T("drafts"), _count(conn, "draft"))
m2.metric(T("approved"), _count(conn, "approved"))
m3.metric(T("submitted"), _count(conn, "submitted"))
m4.metric(T("failed_n"), _count(conn, "failed"))

drafts = fetch_work(conn, status="draft")
tier_choice = st.radio(T("tier_filter"), [T("all"), "Tier 2", "Tier 3"],
                       horizontal=True, label_visibility="collapsed")
if tier_choice == "Tier 2":
    drafts = [d for d in drafts if d.get("tier") == 2]
elif tier_choice == "Tier 3":
    drafts = [d for d in drafts if d.get("tier") == 3]

if not drafts:
    st.info(T("empty"))
else:
    # friction first (least manual work left), then match score — surfaces
    # high-value, low-effort drafts at the top of the queue.
    drafts.sort(key=lambda d: (_friction(d)[0],
                               -(d["job"].get("match_score") or 0)))
    for snap in drafts:
        _draft_card(conn, snap)

approved = fetch_work(conn, status="approved")
if approved:
    st.divider()
    st.subheader(f"{T('approved')} ({len(approved)})")
    st.caption(T("approved_hint"))
    for snap in approved:
        job = snap["job"]
        cols = st.columns([5, 2])
        cols[0].markdown(
            f"**{job.get('company')}** — {job.get('title')} · "
            f"approved {snap.get('approved_at')}")
        if cols[1].button(T("cancel_approval"), key=f"cancel_{snap['id']}"):
            abandon_snapshot(conn, snap["id"], "approval withdrawn")
            st.rerun()
