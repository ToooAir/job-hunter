"""apply_graph.py — per-job LangGraph pipeline for Stage 1 draft generation.

Step 4 architecture is two passes (orchestrated by apply_stage1.py):
  Pass A (browser, container headless) produces per job: verdict + field
  node table + pruned HTML, then closes the page — no browser is held open
  while LLM calls run.
  Pass B runs this graph once per job:

      START ──(has fields?)──► map_deterministic ─► map_llm ─► gen_content
        │                                                          │
        └─────────────► assign_tier ◄────────── verify ◄───────────┘
                            │
                        save_draft ─► END

Jobs whose Pass A verdict yielded no usable field table (external-board,
no-form, nav-error, email-only, shadow-only, account-wall) skip the mapping
chain: tier assignment plus an answer sheet are all we can produce for
them. Captcha pages DO carry a field table — they get a full payload and
Tier 3 ("fill everything, leave the captcha to the human").

Runtime dependencies arrive via config["configurable"]:
  profile      CandidateProfile (required for mapping/verifying/saving)
  db_path      jobs.db path (save_draft)
  dry_run      True → save_draft writes nothing
  qdrant_path  KB store for RAG context; None/missing → no retrieval
  client/model optional LLM overrides (tests inject fakes)
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class ApplyState(TypedDict, total=False):
    # ── Pass A inputs ──────────────────────────────────────────────────
    job: dict                       # queue row + description/cover_letter_draft
    verdict: str                    # ok|captcha|external-board|no-form|...
    apply_url: str                  # final URL the form was found at
    fields: list[dict]              # field node table (FormField.to_dict())
    pruned: dict                    # frame key -> pruned HTML

    # ── form_payload parts (the Step 5 fill instruction set) ───────────
    actions: list[dict]             # {selector, frame_path, kind, label,
                                    #  action: fill|select_option|check|upload,
                                    #  value, source: profile:KEY|llm|file|
                                    #  cover_letter, needs_review}
    unfilled: list[dict]            # {label, selector, reason, required}
    never_fill_skipped: list[str]
    pending: list[dict]             # deterministic leftovers (consumed by map_llm)
    cover_letter_slots: list[dict]  # CL fields found on the form

    # ── generated content ──────────────────────────────────────────────
    open_questions: list[dict]
    custom_qa: list[dict]           # {question, answer, source}
    cover_letter: str               # reused from jobs.cover_letter_draft
    kb_context: str                 # RAG context retrieved once per job

    # ── checks & result ────────────────────────────────────────────────
    verifier_report: dict           # {pass, issues, llm_checked}
    tier: int
    notes: list[str]
    snapshot_id: int                # set by save_draft (unless dry run)


NODE_ORDER = (
    "map_deterministic",
    "map_llm",
    "gen_content",
    "map_agentic",
    "verify",
    "assign_tier",
    "save_draft",
)


def _cfg(config) -> dict:
    return (config or {}).get("configurable", {})


def _draft_view(state: ApplyState) -> dict:
    """The draft as the verifier/tier rules see it."""
    return {
        "actions": state.get("actions") or [],
        "cover_letter": state.get("cover_letter") or "",
        "custom_qa": state.get("custom_qa") or [],
        "unfilled": state.get("unfilled") or [],
    }


def _retrieve_kb(cfg: dict, job: dict) -> tuple[str, str]:
    """(context, note). Retrieval failure degrades to empty context."""
    qdrant_path = cfg.get("qdrant_path")
    if not qdrant_path:
        return "", ""
    try:
        from phase2_scorer import retrieve_context
        jd = job.get("description") or f"{job.get('title', '')} {job.get('company', '')}"
        return retrieve_context(jd, qdrant_path, top_k=5), ""
    except Exception as exc:
        return "", f"kb-retrieval failed: {str(exc)[:100]}"


def map_deterministic(state: ApplyState, config) -> dict:
    from utils.field_mapper import map_fields
    out = map_fields(state.get("fields") or [], _cfg(config)["profile"])
    return {
        "actions": out["actions"],
        "pending": out["pending"],
        "unfilled": out["unfilled"],
        "never_fill_skipped": out["never_fill_skipped"],
    }


def map_llm(state: ApplyState, config) -> dict:
    from utils.apply_llm import map_pending_fields
    cfg = _cfg(config)
    out = map_pending_fields(
        state.get("pending") or [], cfg["profile"], state["job"],
        client=cfg.get("client"), model=cfg.get("model"),
    )
    return {
        "actions": (state.get("actions") or []) + out["actions"],
        "open_questions": out["open_questions"],
        "cover_letter_slots": out["cover_letter_slots"],
        "unfilled": (state.get("unfilled") or []) + out["unfilled"],
        "pending": [],
    }


def gen_content(state: ApplyState, config) -> dict:
    """Reuse the scored cover letter; answer custom questions via LLM+RAG."""
    from utils.apply_llm import answer_open_questions
    from utils.field_mapper import _action

    cfg = _cfg(config)
    job = state["job"]
    cl = (job.get("cover_letter_draft") or "").strip()
    actions = list(state.get("actions") or [])
    unfilled = list(state.get("unfilled") or [])
    notes = list(state.get("notes") or [])

    for slot in state.get("cover_letter_slots") or []:
        if cl:
            actions.append(_action(slot, "fill", cl, "cover_letter", True))
        else:
            unfilled.append({"label": slot.get("label", ""),
                             "selector": slot.get("selector", ""),
                             "reason": "no-cover-letter",
                             "required": bool(slot.get("required"))})

    custom_qa: list[dict] = []
    kb_context = state.get("kb_context") or ""
    questions = state.get("open_questions") or []
    if questions:
        if not kb_context:
            kb_context, note = _retrieve_kb(cfg, job)
            if note:
                notes.append(note)
        out = answer_open_questions(questions, job, kb_context,
                                    client=cfg.get("client"), model=cfg.get("model"))
        actions += out["actions"]
        custom_qa = out["custom_qa"]
        unfilled += out["unfilled"]

    return {"actions": actions, "unfilled": unfilled, "custom_qa": custom_qa,
            "cover_letter": cl, "kb_context": kb_context, "notes": notes}


def map_agentic(state: ApplyState, config) -> dict:
    """Hybrid fallback: the agentic mapper fills the long tail the deterministic
    + LLM passes left empty. It sees the WHOLE field table (whole-page context
    helps it answer — e.g. lever's context_hint questions the rule passes drop),
    but only its actions for STILL-UNFILLED selectors are adopted; deterministic
    / LLM actions stay authoritative. Every adopted action is needs_review, so
    the draft can only reach Tier 2 (never auto-submit), and its free text is
    audited by verify_draft's fabrication gate downstream.

    Opt-out via config enable_agentic_fallback (default on). LLM-only; costs
    +1 Mistral call per job, and only when an unfilled gap actually exists.
    """
    cfg = _cfg(config)
    if not cfg.get("enable_agentic_fallback", True):
        return {}
    fields = state.get("fields") or []
    actions = list(state.get("actions") or [])
    actioned = {a.get("selector") for a in actions}
    if not any(f.get("selector") not in actioned for f in fields):
        return {}  # deterministic + LLM already covered every field — no call

    from utils.agentic_mapper import map_page_agentic
    out = map_page_agentic(fields, cfg["profile"], state["job"],
                           client=cfg.get("client"), model=cfg.get("model"))
    adopted = [a for a in out.get("actions", [])
               if a.get("selector") and a.get("selector") not in actioned]
    if not adopted:
        return {}

    adopted_sels = {a["selector"] for a in adopted}
    adopted_vals = {a.get("value") for a in adopted}
    unfilled = [u for u in (state.get("unfilled") or [])
                if u.get("selector") not in adopted_sels]
    custom_qa = list(state.get("custom_qa") or []) + [
        q for q in out.get("custom_qa", []) if q.get("answer") in adopted_vals]
    notes = list(state.get("notes") or [])
    notes.append(f"agentic fallback: +{len(adopted)} gap fill(s)")
    return {"actions": actions + adopted, "unfilled": unfilled,
            "custom_qa": custom_qa, "notes": notes}


def verify(state: ApplyState, config) -> dict:
    from utils.apply_verifier import verify_draft
    cfg = _cfg(config)
    draft = _draft_view(state)
    notes = list(state.get("notes") or [])

    kb_context = state.get("kb_context") or ""
    has_generated = bool(draft["cover_letter"] or draft["custom_qa"]
                         or any(a.get("source") == "llm" for a in draft["actions"]))
    if has_generated and not kb_context:
        kb_context, note = _retrieve_kb(cfg, state["job"])
        if note:
            notes.append(note)

    report = verify_draft(draft, cfg["profile"], state["job"],
                          kb_context=kb_context,
                          client=cfg.get("client"), model=cfg.get("model"))
    return {"verifier_report": report, "kb_context": kb_context, "notes": notes}


def assign_tier(state: ApplyState, config) -> dict:
    from utils.apply_verifier import assign_tier as _assign
    tier, reasons = _assign(
        state.get("verdict") or "ok",
        state["job"],
        _draft_view(state),
        state.get("verifier_report"),
        dedup=state["job"].get("dedup", "ok"),
    )
    notes = list(state.get("notes") or [])
    notes += [f"tier{tier}: {r}" for r in reasons]
    return {"tier": tier, "notes": notes}


def save_draft(state: ApplyState, config) -> dict:
    cfg = _cfg(config)
    job = state["job"]
    payload = {
        "actions": state.get("actions") or [],
        "unfilled": state.get("unfilled") or [],
        "never_fill_skipped": state.get("never_fill_skipped") or [],
    }
    if not payload["actions"] and cfg.get("profile") is not None:
        # nothing fillable here (Tier 3 page) — store the answer sheet the
        # human uses to finish the application manually
        payload["answer_sheet"] = {
            f.key: f.value for f in cfg["profile"].fields.values()
        }

    # Every draft starts in review; there is no auto-submission path, so the
    # human always reads it and applies on the real site themselves. The tier
    # is still recorded — it tells the reviewer how much judgment a draft needs.
    notes = list(state.get("notes") or [])

    if cfg.get("dry_run"):
        return {"notes": notes + ["save_draft: dry-run, no write"]}

    from utils.db import create_application_snapshot, init_db
    conn = init_db(cfg["db_path"])
    try:
        snapshot_id = create_application_snapshot(
            conn,
            job_id=job["id"],
            status="draft",
            tier=state.get("tier"),
            channel=("company-form" if (state.get("verdict") or "ok") == "ok"
                     else state.get("verdict")),
            apply_url=state.get("apply_url") or job.get("apply_url") or job.get("url"),
            form_payload=payload,
            cover_letter=state.get("cover_letter") or "",
            custom_qa=state.get("custom_qa") or [],
            verifier_report=state.get("verifier_report") or {},
            notes="; ".join(notes),
        )
    finally:
        conn.close()
    return {"snapshot_id": snapshot_id}


def _route_entry(state: ApplyState) -> str:
    """Mapping chain runs when there is a real field table to fill: 'ok',
    or 'captcha' (full payload, human presses the button). Junk tables
    (weak-form = search bars etc.) skip straight to tier assignment."""
    mappable = state.get("verdict") in (None, "", "ok", "captcha")
    return "map" if state.get("fields") and mappable else "tier"


def build_graph(overrides: dict | None = None):
    """Compile the per-job graph.

    `overrides` swaps node implementations by name — used by routing tests
    (recorder nodes) and available for future special-case handling.
    """
    impl = {name: globals()[name] for name in NODE_ORDER}
    if overrides:
        unknown = set(overrides) - set(NODE_ORDER)
        if unknown:
            raise ValueError(f"unknown node override(s): {sorted(unknown)}")
        impl.update(overrides)

    g = StateGraph(ApplyState)
    for name in NODE_ORDER:
        g.add_node(name, impl[name])
    g.add_conditional_edges(
        START, _route_entry,
        {"map": "map_deterministic", "tier": "assign_tier"},
    )
    g.add_edge("map_deterministic", "map_llm")
    g.add_edge("map_llm", "gen_content")
    g.add_edge("gen_content", "map_agentic")
    g.add_edge("map_agentic", "verify")
    g.add_edge("verify", "assign_tier")
    g.add_edge("assign_tier", "save_draft")
    g.add_edge("save_draft", END)
    return g.compile()
