"""apply_graph.py — per-job LangGraph pipeline for Stage 1 draft generation.

Step 4 architecture is two passes (orchestrated by apply_stage1.py):
  Pass A (browser, container headless) verifies the apply page is alive and
  classifies it (verdict), then closes — no browser is held open while LLM
  calls run.
  Pass B runs this graph once per job:

      START ──(verdict ok/captcha?)──► gen_content ─► verify
        │                                               │
        └────────────► assign_tier ◄────────────────────┘
                           │
                       save_draft ─► END

The field-mapping chain (map_deterministic → map_llm → map_agentic) was
retired 2026-07-02: its selector-replay payload is superseded by the
extension's live extraction (POST /fill-plan), and its pre-generated
per-form answers were used by 0 of 74 historical snapshots' submissions.
Facts are filled live at apply time; open questions are answered by the
human (the cover letter below is their raw material). The mapper modules
(field_mapper, agentic_mapper, the mapping half of apply_llm) and the
agentic spike tools were deleted with it — git history keeps them.

Jobs whose verdict is junk (external-board, no-form, nav-error, email-only,
shadow-only, account-wall) skip straight to tier assignment: tier plus an
answer sheet are all we can produce for them.

Runtime dependencies arrive via config["configurable"]:
  profile      CandidateProfile (required for verifying/saving)
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
    fields: list[dict]              # Pass A field table — kept for the verdict
    #                                 (weak-form detection); no longer mapped
    pruned: dict                    # frame key -> pruned HTML

    # ── legacy form_payload keys (mapping chain retired; kept because the
    #    verifier/save_draft read them with empty defaults) ──────────────
    actions: list[dict]
    unfilled: list[dict]
    never_fill_skipped: list[str]

    # ── generated content ──────────────────────────────────────────────
    custom_qa: list[dict]           # {question, answer, source}
    cover_letter: str               # reused from jobs.cover_letter_draft
    kb_context: str                 # RAG context retrieved once per job

    # ── checks & result ────────────────────────────────────────────────
    verifier_report: dict           # {pass, issues, llm_checked}
    tier: int
    notes: list[str]
    snapshot_id: int                # set by save_draft (unless dry run)


NODE_ORDER = (
    "gen_content",
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


def gen_content(state: ApplyState, config) -> dict:
    """Carry the scored cover letter into the draft — the cheat sheet's raw
    material for the human's open answers. Per-form question answering left
    with the retired mapping chain (see module docstring)."""
    cl = (state["job"].get("cover_letter_draft") or "").strip()
    return {"cover_letter": cl}


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
    """Content + verification run for real apply pages ('ok', or 'captcha' —
    human presses the button). Junk verdicts (weak-form = search bars,
    external-board, no-form, …) skip straight to tier assignment."""
    return "gen" if state.get("verdict") in (None, "", "ok", "captcha") else "tier"


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
        {"gen": "gen_content", "tier": "assign_tier"},
    )
    g.add_edge("gen_content", "verify")
    g.add_edge("verify", "assign_tier")
    g.add_edge("assign_tier", "save_draft")
    g.add_edge("save_draft", END)
    return g.compile()
