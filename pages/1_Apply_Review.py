"""Apply review page — the draft review queue.

Streamlit multipage: lives in pages/ next to phase3_dashboard.py and shows
up in the sidebar nav automatically. All snapshot reads/writes go through
utils.snapshot_io so the lifecycle rules hold no matter where a decision
is made.

Each draft is reviewed and optionally fine-tuned, then the human copies the
answer sheet (st.code = built-in copy button) onto the real application form,
submits it themselves, and marks it submitted here (which books the job as
applied). There is no automated submission step.
"""

import json
import os
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

from utils.db import get_focus, init_db, set_focus
from utils.snapshot_io import (
    abandon_snapshot,
    edit_snapshot,
    fetch_work,
    mark_submitted,
)

st.set_page_config(layout="wide", page_title="Apply Review")

_STRINGS = {
    "zh": {
        "title": "投遞審閱佇列",
        "intro": "Stage 1 草稿在此審閱、可微調。把內容複製到真實申請頁、自己送出後,"
                 "按「標記已投遞」入帳。Tier 3 同樣用小抄手投。",
        "drafts": "待審草稿", "submitted": "已投遞", "abandoned": "已放棄",
        "empty": "目前沒有待審草稿。先跑 apply_stage1.py。",
        "tier_filter": "Tier 篩選", "all": "全部",
        "match": "匹配", "created": "建立於", "channel": "管道",
        "verifier_pass": "verifier 通過(全確定性,無生成內容需查)",
        "verifier_cleared": "✓ verifier 已獨立查核生成內容的事實(無捏造／洩漏／薪資・簽證一致)。"
                            "你只需判斷:語氣像不像你、適不適合這家公司 — 不必重查事實。",
        "cl_endorsed": "✓ 事實已查核。你只需判斷語氣與適配。",
        "verifier_skip": "verifier 未跑(無生成內容)",
        "verifier_blocking": "需處理的阻擋問題", "verifier_minor": "次要提醒",
        "friction_easy": "易投", "friction_medium": "需審", "friction_painful": "填好待按",
        "friction_account": "需手動/帳號",
        "needs_judgment": "需要你判斷",
        "edit_hint": "可直接微調下列內容,存檔後再從小抄複製到真實申請頁",
        "salary_field": "薪資(不在此改,改 profile)",
        "auto_filled": "個 profile 欄位(確定性,複製即可)",
        "tab_cl": "Cover Letter", "tab_qa": "自訂問答",
        "tab_sheet": "小抄(點右上複製)",
        "cl_flagged": "這封 cover letter 被標記的疑慮(送出前請確認)",
        "no_actions": "無自動填值(Tier 3 頁面)",
        "notes": "備註",
        "save_edits": "存檔(供複製)", "mark_submitted": "標記已投遞",
        "abandon": "放棄", "abandon_reason": "放棄原因(選填)",
        "required_mark": "必填",
        "docs_needed": "此表單需你手動附上文件",
        "doc_cl_hint": "(求職信,下方已有文字可貼/另存 PDF)",
        "verified_ago": "已驗證可投(約 {} 天前)",
        "verified_today": "今天已驗證可投",
        "liveness_suspect": "可疑:重驗時不是乾淨的申請表(帳號牆/captcha/弱表單)——投前自行確認職缺還在",
        "never_verified": "尚未重驗失效",
        "set_focus": "我要投這筆",
        "focus_now": "🎯 投遞中:{} — {}({})— 插件答案面板以此職缺為根據",
    },
    "en": {
        "title": "Apply Review Queue",
        "intro": "Review and fine-tune Stage 1 drafts here. Copy the content onto the "
                 "real application form, submit it yourself, then click 'Mark submitted'. "
                 "Tier 3 uses the same answer sheet.",
        "drafts": "Drafts", "submitted": "Submitted", "abandoned": "Abandoned",
        "empty": "No drafts to review. Run apply_stage1.py first.",
        "tier_filter": "Tier filter", "all": "All",
        "match": "Match", "created": "Created", "channel": "Channel",
        "verifier_pass": "verifier passed (fully deterministic, nothing generated to audit)",
        "verifier_cleared": "✓ Verifier independently checked the generated content "
                            "(no fabrication / leak / salary or visa mismatch). Your call "
                            "is only: does it sound like you, and fit this company? — no "
                            "need to re-verify facts.",
        "cl_endorsed": "✓ Facts checked. Your call is only voice and fit.",
        "verifier_skip": "verifier skipped (nothing generated)",
        "verifier_blocking": "Blocking issues", "verifier_minor": "Minor notes",
        "friction_easy": "easy", "friction_medium": "review", "friction_painful": "fill+press",
        "friction_account": "manual/account",
        "needs_judgment": "Needs your judgment",
        "edit_hint": "Fine-tune below, save, then copy from the sheet onto the real form",
        "salary_field": "Salary (edit in profile, not here)",
        "auto_filled": "profile fields (deterministic, just copy)",
        "tab_cl": "Cover Letter", "tab_qa": "Custom Q&A",
        "tab_sheet": "Answer sheet (copy top-right)",
        "cl_flagged": "Flags on this cover letter (confirm before submitting)",
        "no_actions": "No automatic fills (Tier 3 page)",
        "notes": "Notes",
        "save_edits": "Save (for copying)", "mark_submitted": "Mark submitted",
        "abandon": "Abandon", "abandon_reason": "Reason (optional)",
        "required_mark": "required",
        "docs_needed": "This form needs you to attach",
        "doc_cl_hint": " (cover letter — text is ready below to paste / save as PDF)",
        "verified_ago": "verified live ~{}d ago",
        "verified_today": "verified live today",
        "liveness_suspect": "suspect: not a clean application form on recheck "
                            "(account wall / captcha / weak form) — confirm the posting before applying",
        "never_verified": "not liveness-checked yet",
        "set_focus": "applying to this one",
        "focus_now": "🎯 applying: {} — {} ({}) — the answer panel grounds on this job",
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


def _cl_endorsed(report: dict | None) -> bool:
    """True when the independent verifier actually reviewed the generated text
    and cleared it (no high-severity cover-letter flag). The human's remaining
    job is voice/fit, not re-checking facts the verifier already grounded."""
    return bool(isinstance(report, dict) and report.get("llm_checked")
                and report.get("pass") and not _cl_flags(report))


def _verifier_block(report: dict | None) -> None:
    """Triage display (watchlist #13): blocking issues stay loud and red,
    minor notes collapse into a muted expander so a draft with only low-
    severity nits doesn't read like a failure (alarm-fatigue fix)."""
    if not isinstance(report, dict) or not report:
        st.caption(T("verifier_skip"))
        return
    issues = report.get("issues") or []
    if report.get("pass") and not issues:
        # llm_checked => the verifier actually audited generated free text;
        # reframe the human's job to voice/fit. Otherwise the draft was purely
        # deterministic and there was nothing to audit in the first place.
        st.success(T("verifier_cleared") if report.get("llm_checked")
                   else T("verifier_pass"))
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


# Sources whose value the human must actually read before approving. Everything
# else is a deterministic copy from the profile (name, email, CV) — re-checking
# those one by one is the fatigue the one-glance card removes.
_ATTENTION_SOURCES = {"llm", "profile:salary_expectation"}

# Upload slots nothing can fill automatically: the form wants extra documents
# (Zeugnisse, references, a cover-letter PDF). Legacy drafts (pre-2026-07-02,
# retired field_mapper) carry these reasons in `unfilled`; new drafts have an
# empty payload, so this renders nothing for them.
_DOC_SLOT_REASONS = {"attachment-unmapped", "cover-letter-upload"}


def _doc_slots(payload: dict) -> list[dict]:
    """The document slots the human must supply by hand. Surfaced up front so
    the user gathers the files before starting, instead of hitting a missing-
    document wall mid-application (watchlist #7)."""
    return [u for u in (payload.get("unfilled") or [])
            if u.get("reason") in _DOC_SLOT_REASONS]


def _doc_notice(payload: dict) -> None:
    """One amber line listing the documents this form expects you to attach."""
    slots = _doc_slots(payload)
    if not slots:
        return
    parts = []
    for u in slots:
        label = u.get("label") or u.get("selector") or "document"
        if u.get("reason") == "cover-letter-upload":
            label += T("doc_cl_hint")
        if u.get("required"):
            label += f" ({T('required_mark')})"
        parts.append(label)
    st.warning(f"📎 {T('docs_needed')}: " + " · ".join(parts))


def _is_attention(a: dict) -> bool:
    """A fill that needs a human's judgment: LLM-generated text, the salary
    field (never auto-submit), or a value the extractor flagged as a possible
    label/value mismatch (needs_review)."""
    return a.get("source") in _ATTENTION_SOURCES or bool(a.get("needs_review"))


def _fill_rows(actions: list[dict]) -> list[dict]:
    return [{"label": a.get("label"), "value": a.get("value"),
             "source": a.get("source"),
             "review": "⚠️" if a.get("needs_review") else ""}
            for a in actions]


def _is_editable_field(a: dict) -> bool:
    """A flagged form field a reviewer may correct in place: LLM-generated or
    needs_review. Salary stays read-only (it is a profile value, edited in the
    profile, not per-application); the cover letter has its own editor."""
    return ((a.get("source") == "llm" or a.get("needs_review"))
            and a.get("source") != "cover_letter")


def _fills_section(snap: dict, payload: dict, editable: bool = False) -> dict:
    """One-glance fill plan: the 1-2 fields needing judgment float to the top
    (red), the deterministic profile copies collapse behind a toggle. The card
    is itself an st.expander, so the sub-collapse uses st.toggle — Streamlit
    forbids nesting expanders.

    When editable, flagged fields render as text inputs; returns
    {selector: value} of those inputs so the caller can persist edits on
    approve. Read-only mode returns an empty dict."""
    actions = [a for a in (payload.get("actions") or [])
               if a.get("source") != "cover_letter"]  # letter has its own section
    answered = {q.get("answer", "") for q in snap.get("custom_qa") or []}
    # an LLM value that is also a custom_qa answer is shown in the Q&A section;
    # don't list it here too, or the same text reads as two separate concerns.
    attention = [a for a in actions if _is_attention(a)
                 and not (a.get("source") == "llm" and a.get("value") in answered)]
    auto = [a for a in actions if not _is_attention(a)]
    # document slots (CV/Zeugnisse/CL-PDF) have their own up-front notice
    # (_doc_notice); keep them out of here so they aren't listed twice.
    req_unfilled = [u for u in (payload.get("unfilled") or [])
                    if u.get("required") and u.get("reason") not in _DOC_SLOT_REASONS]
    edits: dict = {}

    if not actions and not req_unfilled:
        st.caption(T("no_actions"))

    if attention or req_unfilled:
        st.markdown(f"**🔴 {T('needs_judgment')} "
                    f"({len(attention) + len(req_unfilled)})**")
        editable_fields = [a for a in attention if _is_editable_field(a)]
        readonly = [a for a in attention if not _is_editable_field(a)]
        if editable and editable_fields:
            st.caption(T("edit_hint"))
            for a in editable_fields:
                sel = a.get("selector")
                edits[sel] = st.text_input(
                    a.get("label") or sel, value=a.get("value") or "",
                    key=f"fld_{snap['id']}_{sel}")
        elif editable_fields:
            st.dataframe(_fill_rows(editable_fields), width="stretch",
                         hide_index=True)
        for a in readonly:  # salary: surfaced, never auto-submitted, not edited
            st.caption(f"{T('salary_field')}: {a.get('value')}")
        for u in req_unfilled:
            st.caption(f"• {u.get('label') or u.get('selector')} "
                       f"({T('required_mark')}) — {u.get('reason')}")

    if auto and st.toggle(f"✓ {len(auto)} {T('auto_filled')}",
                          key=f"auto_{snap['id']}"):
        st.dataframe(_fill_rows(auto), width="stretch", hide_index=True)

    return edits


def _cover_letter_section(snap: dict, editable: bool = False) -> str | None:
    """Render the cover letter with its verifier endorsement / flags. When
    editable, returns the (possibly edited) text from a text area so the caller
    can persist it on approve; read-only mode returns None."""
    if not snap.get("cover_letter"):
        return None
    st.markdown(f"**{T('tab_cl')}**")
    flags = _cl_flags(snap.get("verifier_report"))
    if flags:
        st.error(f"⚠️ {T('cl_flagged')}")
        for issue in flags:
            st.error(_issue_line(issue))
    elif _cl_endorsed(snap.get("verifier_report")):
        st.success(T("cl_endorsed"))
    if editable:
        return st.text_area(T("tab_cl"), value=snap["cover_letter"], height=260,
                            key=f"cl_{snap['id']}", label_visibility="collapsed")
    st.code(snap["cover_letter"], language=None)
    return None


def _qa_section(snap: dict) -> None:
    if not snap.get("custom_qa"):
        return
    st.markdown(f"**{T('tab_qa')}**")
    for qa in snap["custom_qa"]:
        st.markdown(f"*{qa.get('question', '')}*")
        st.code(qa.get("answer", ""), language=None)


def _sheet_tab(snap: dict, payload: dict) -> None:
    """Every value as its own st.code block — one click per copy."""
    slots = _doc_slots(payload)
    if slots:  # remind the human which files to attach while they're on the form
        st.caption(f"📎 {T('docs_needed')}")
        st.code("\n".join(
            (u.get("label") or u.get("selector") or "document")
            + (f" ({T('required_mark')})" if u.get("required") else "")
            for u in slots), language=None)
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


def _liveness_caption(snap: dict) -> None:
    """Show how recently the draft was confirmed live, and a warning if the
    liveness sweep flagged it suspicious — so a glance over the queue tells you
    what's still applicable (the sweep prunes the clearly-dead automatically)."""
    checked = (snap.get("job") or {}).get("ats_checked_at")
    age = None
    if checked:
        try:
            age = (datetime.now() - datetime.fromisoformat(checked.replace("Z", ""))).days
        except ValueError:
            age = None
    if snap.get("liveness") == "suspicious":
        suffix = f" ({T('verified_ago').format(age)})" if age is not None else ""
        st.warning(f"⚠️ {T('liveness_suspect')}{suffix}")
    elif age is not None:
        st.caption("✓ " + (T("verified_today") if age <= 0 else T("verified_ago").format(age)))
    elif checked is None:
        st.caption(T("never_verified"))


def _draft_card(conn, snap: dict) -> None:
    job = snap["job"]
    payload = snap.get("form_payload") or {}
    tier = snap.get("tier")
    score = job.get("match_score")
    _, friction_label = _friction(snap)
    header = (f"T{tier} [{friction_label}] · {job.get('company')} — {job.get('title')}"
              + (f" · {T('match')} {score}" if score is not None else ""))
    # the 🎯 rerun (below) collapses every expander; keep the card the user is
    # actively applying from open, or they must re-open it to reach the URL
    with st.expander(header,
                     expanded=st.session_state.get("keep_open") == snap["id"]):
        cap, focus_col = st.columns([4, 1])
        cap.caption(f"{T('channel')}: {snap.get('channel')} · "
                    f"{T('created')}: {snap.get('created_at')} · "
                    f"[{snap.get('apply_url')}]({snap.get('apply_url')})")
        # answer-panel focus (ANSWER_PANEL_PLAN.md): the explicit "I am
        # applying to THIS one" signal — host matching can't identify the
        # job (same-ATS drafts collide, redirects change the host)
        if focus_col.button(f"🎯 {T('set_focus')}", key=f"focus_{snap['id']}"):
            set_focus(conn, snap["id"], snap["job_id"])
            st.session_state["keep_open"] = snap["id"]
            st.rerun()

        _liveness_caption(snap)
        _verifier_block(snap.get("verifier_report"))
        _doc_notice(payload)  # documents to attach by hand, before anything else

        # One scannable card, ordered by how much judgment each part needs:
        # flagged fills first, then generated free text (voice/fit), then the
        # deterministic fills and answer sheet collapsed out of the way. The
        # human copies this onto the real form; flagged fields + cover letter
        # are editable so the copy reflects any fix. Tier 3 is read-only sheet.
        editable = tier != 3
        field_edits = _fills_section(snap, payload, editable=editable)
        cl_new = _cover_letter_section(snap, editable=editable)
        _qa_section(snap)
        manual = not (payload.get("actions"))  # Tier 3 copy-paste path
        if st.toggle(T("tab_sheet"), key=f"sheet_{snap['id']}", value=manual):
            _sheet_tab(snap, payload)

        # Spike harness (SPIKE_PLAN.md): the raw form_payload as one-click-copy
        # JSON, fed to the autofill extension's clipboard reader. Additive only.
        if payload.get("actions") and st.toggle(
                "🧪 Spike: payload JSON", key=f"spike_{snap['id']}"):
            st.code(json.dumps(payload, ensure_ascii=False), language="json")

        if snap.get("notes"):
            st.caption(f"{T('notes')}: {snap['notes']}")

        # Save persists edits so the answer sheet shows the corrected text to
        # copy; Mark submitted books the application after the human applied.
        cols = st.columns([1, 1, 1, 2])
        if editable and cols[0].button(
                T("save_edits"), key=f"save_{snap['id']}"):
            edit_snapshot(conn, snap["id"], cover_letter=cl_new,
                          action_values=field_edits)
            st.session_state["keep_open"] = snap["id"]
            st.rerun()
        if cols[1].button(T("mark_submitted"), key=f"submit_{snap['id']}",
                          type="primary"):
            if editable:
                edit_snapshot(conn, snap["id"], cover_letter=cl_new,
                              action_values=field_edits)
            mark_submitted(conn, snap["id"], note="marked submitted in review")
            st.rerun()
        reason = cols[3].text_input(
            T("abandon_reason"), key=f"reason_{snap['id']}",
            label_visibility="collapsed", placeholder=T("abandon_reason"))
        if cols[2].button(T("abandon"), key=f"abandon_{snap['id']}"):
            abandon_snapshot(conn, snap["id"], reason)
            st.rerun()


conn = get_conn()
st.title(T("title"))
st.caption(T("intro"))


# The extension books submissions through apply_api, outside this page's
# reruns — poll a cheap queue signature and pull the whole page forward when
# it changes, so a just-submitted draft disappears without a manual refresh.
@st.fragment(run_every="3s")
def _queue_watch():
    c = get_conn()  # own connection: fragment reruns run outside the page's thread
    try:
        sig = tuple(r[0] for r in c.execute(
            "SELECT id FROM application_snapshots WHERE status='draft' ORDER BY id"))
    finally:
        c.close()
    prev = st.session_state.get("queue_sig")
    st.session_state["queue_sig"] = sig
    if prev is not None and prev != sig:
        st.rerun(scope="app")


_queue_watch()

# current answer-panel focus — visible so a stale 🎯 can't misground quietly
_focus = get_focus(conn)
if _focus:
    _fjob = conn.execute("SELECT company, title FROM jobs WHERE id = ?",
                         (_focus["job_id"],)).fetchone()
    if _fjob:
        st.info(T("focus_now").format(
            _fjob["company"], _fjob["title"], _focus["updated_at"][11:16]))

m1, m2, m3 = st.columns(3)
m1.metric(T("drafts"), _count(conn, "draft"))
m2.metric(T("submitted"), _count(conn, "submitted"))
m3.metric(T("abandoned"), _count(conn, "abandoned"))

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
