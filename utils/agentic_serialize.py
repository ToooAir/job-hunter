"""agentic_serialize.py — render a field node table as an ID-tagged text block.

The agentic mapper (Step 6.A experiment) lets the LLM drive the whole page at
once instead of the deterministic mapper placing one field at a time. For that
the LLM needs to *see* the page as text and refer to controls by a stable
handle. This module is the "page → text" half: it turns the dom_pruner field
table into a numbered list where the number IS the handle.

Grounding contract: the id the LLM emits is the field's index in the SAME list
that was serialized, so `ground(fields, id)` is a bounds-checked lookup — no
new element-location logic, the pruner's selector is reused verbatim.

Pure module: stdlib only, no LLM/browser/DB. Unit-testable anywhere.
"""

from __future__ import annotations

_MAX_OPTIONS_SHOWN = 25


def _field_line(idx: int, f: dict) -> str:
    """One `[idx] kind "label" — flags` line for the LLM."""
    kind = f.get("kind", "text")
    label = (f.get("label") or "").strip() or f.get("name") or "(no label)"
    line = f'[{idx}] {kind} "{label}"'

    flags: list[str] = []
    if f.get("required"):
        flags.append("required")
    if f.get("label_suspect"):
        # the pruner guessed this label positionally — tell the LLM so it can
        # use page order / nearby questions to disambiguate (milia lesson)
        flags.append("label-uncertain")
    if kind == "custom":
        flags.append("custom-widget (no preset options)")
    if f.get("accept"):
        flags.append(f"accepts {f['accept']}")

    opts = f.get("options") or []
    if opts:
        shown = opts[:_MAX_OPTIONS_SHOWN]
        more = "" if len(opts) <= _MAX_OPTIONS_SHOWN else f", …(+{len(opts) - _MAX_OPTIONS_SHOWN})"
        flags.append("options: " + " | ".join(shown) + more)

    return line + ("  —  " + "; ".join(flags) if flags else "")


def serialize_fields(fields: list[dict]) -> str:
    """The whole field table as a numbered text block (one line per field)."""
    return "\n".join(_field_line(i, f) for i, f in enumerate(fields))


def ground(fields: list[dict], idx) -> dict | None:
    """The field an LLM-emitted id refers to, or None if the id is bogus.

    A bogus id (out of range, non-int) is the LLM hallucinating a control that
    is not on the page — the caller drops it rather than acting on nothing.
    """
    if isinstance(idx, bool) or not isinstance(idx, int):
        return None
    if 0 <= idx < len(fields):
        return fields[idx]
    return None
