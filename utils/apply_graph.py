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
no-form, nav-error, email-only, shadow-only) skip the mapping chain: tier
assignment plus an answer sheet are all we can produce for them. Captcha
pages DO carry a field table — they get a full payload and Tier 3
("fill everything, leave the captcha to the human").

This module owns the state contract and the wiring. Node bodies land in
later sub-tasks: 4.1 map_deterministic, 4.2 map_llm/gen_content,
4.3 verify/assign_tier, 4.4 save_draft.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class ApplyState(TypedDict, total=False):
    # ── Pass A inputs ──────────────────────────────────────────────────
    job: dict                       # queue row (id, company, title, ats, dedup, ...)
    verdict: str                    # ok|captcha|external-board|no-form|...
    apply_url: str                  # final URL the form was found at
    fields: list[dict]              # field node table (FormField.to_dict())
    pruned: dict                    # frame key -> pruned HTML (LLM context)

    # ── form_payload parts (the Step 5 fill instruction set) ───────────
    actions: list[dict]             # {selector, frame_path, kind, label,
                                    #  action: fill|select_option|check|upload|skip,
                                    #  value, source: profile:KEY|llm|file,
                                    #  needs_review}
    unfilled: list[dict]            # {label, reason, required}
    never_fill_skipped: list[str]
    pending: list[dict]             # fields the deterministic mapper left for the LLM

    # ── generated content ──────────────────────────────────────────────
    open_questions: list[dict]      # custom questions found on the form
    custom_qa: list[dict]           # {question, answer, source}
    cover_letter: str               # reused from jobs.cover_letter_draft

    # ── checks & result ────────────────────────────────────────────────
    verifier_report: dict           # {pass, issues: [...]}
    tier: int                       # 1|2|3
    notes: list[str]
    snapshot_id: int                # set by save_draft (unless dry run)


NODE_ORDER = (
    "map_deterministic",
    "map_llm",
    "gen_content",
    "verify",
    "assign_tier",
    "save_draft",
)


def _note(state: ApplyState, text: str) -> dict:
    return {"notes": list(state.get("notes") or []) + [text]}


# Node bodies are stubs until their sub-task lands; each one records that it
# ran so the orchestrator's accounting never loses a job silently.

def map_deterministic(state: ApplyState) -> dict:  # 4.1
    return _note(state, "map_deterministic: stub")


def map_llm(state: ApplyState) -> dict:  # 4.2
    return _note(state, "map_llm: stub")


def gen_content(state: ApplyState) -> dict:  # 4.2
    return _note(state, "gen_content: stub")


def verify(state: ApplyState) -> dict:  # 4.3
    return _note(state, "verify: stub")


def assign_tier(state: ApplyState) -> dict:  # 4.3
    return _note(state, "assign_tier: stub")


def save_draft(state: ApplyState, config) -> dict:  # 4.4 (db_path/dry_run via config)
    return _note(state, "save_draft: stub")


def _route_entry(state: ApplyState) -> str:
    """Mapping chain only makes sense when Pass A extracted a field table."""
    return "map" if state.get("fields") else "tier"


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
    g.add_edge("gen_content", "verify")
    g.add_edge("verify", "assign_tier")
    g.add_edge("assign_tier", "save_draft")
    g.add_edge("save_draft", END)
    return g.compile()
