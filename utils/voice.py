"""utils/voice.py — the candidate's own words, appended to every KB context.

Why this exists
---------------
Cover letters built only from resume_bullets.md / projects.md read as
machine-written, because those files hold nothing but achievements: verb-initial,
metric-forward, already compressed. That register IS the LLM register, so the
letter comes out as a restatement of the bullets ("I bring expertise in Python,
GCP and SQL ... where I reduced database size by half and automated processes
saving 160+ hours monthly" — near-verbatim resume_bullets.md).

What a human letter adds is material no résumé holds: why this kind of work,
what a metric actually changed for the team, which problems the candidate likes.
The model cannot supply that without inventing it, and the verifier would
(correctly) flag the invention. So it has to become a KB *input* — voice.md
holds the candidate's own statements, and grounded personal texture stops being
a fabrication.

Why it is injected, not retrieved
---------------------------------
voice.md deliberately does NOT go into Qdrant (see kb_loader.build_kb):

1. Voice text is semantically far from any job description, so a top-k
   similarity search would never surface it — the whole point is that it is
   about the candidate, not about the role.
2. Even if it did rank, a "why I like this work" chunk would displace a
   relevant project from the 3–5 available slots.

Instead it is appended inside _qdrant_query — the one function every KB context
string in the system passes through (batch scoring, single-job regeneration, and
the Stage-1 verifier's retrieval). Hooking there guarantees the generator and
the verifier see the SAME voice material. When those two diverge, the verifier
flags the candidate's real motivation as an unsupported claim — the failure mode
already recorded on 2026-07-10.

The file is optional: with no voice.md the context is returned untouched and
every existing behaviour is unchanged.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_VOICE_PATH = PROJECT_ROOT / "candidate_kb" / "voice.md"

# Same "[來源: x]" shape _qdrant_query gives retrieved chunks, so the prompt can
# name this block ("when the background contains a [來源: voice.md] block ...")
# and the LLM can tell first-person source material from third-person JD text.
VOICE_HEADER = "[來源: voice.md — 候選人自述,可直接取材]"

_SEPARATOR = "\n---\n"

# Everything above the first `---` line is notes to the human (what the file is
# for, how to write it) and must not reach the LLM as if it were the candidate
# speaking. Same for TODO blocks: they name the gaps the candidate has not
# filled yet, and an unfiltered "TODO — why Germany, in your own words" reads to
# the generator as an instruction to write exactly that, ungrounded.
_FRONT_MATTER_FENCE = "---"
_TODO_PREFIX = "todo"


def _strip_notes(text: str) -> str:
    """Drop the human-facing preamble and any unfilled TODO blocks."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _FRONT_MATTER_FENCE:
            lines = lines[i + 1:]
            break

    blocks = [
        b for b in "\n".join(lines).split("\n\n")
        if b.strip() and not b.strip().lower().startswith(_TODO_PREFIX)
    ]

    # A section whose only content was a TODO leaves a bare heading behind. Left
    # in, it reads as a prompt to fill the gap — the exact ungrounded writing the
    # verifier exists to catch.
    kept: list[str] = []
    for i, block in enumerate(blocks):
        is_heading = block.strip().startswith("#")
        next_is_heading = (
            i + 1 < len(blocks) and blocks[i + 1].strip().startswith("#")
        )
        if is_heading and (i + 1 == len(blocks) or next_is_heading):
            continue
        kept.append(block)
    return "\n\n".join(kept).strip()

# (path, mtime, size) -> text. Keyed on the stat so editing voice.md takes
# effect on the next generation without restarting apply_api — unlike the
# lru_cache on the profile loader, which needs a restart to pick up edits.
_cache: dict[str, tuple[float, int, str]] = {}


def load_voice(path: str | Path | None = None) -> str:
    """voice.md's usable text, or "" when the file is absent or blank.

    Notes to the human are stripped: the preamble above the first `---` line and
    any TODO block. What is returned is only what the candidate actually said.
    """
    p = Path(path) if path else DEFAULT_VOICE_PATH
    try:
        stat = p.stat()
    except OSError:
        return ""

    key = str(p)
    cached = _cache.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    try:
        text = _strip_notes(p.read_text(encoding="utf-8"))
    except OSError:
        return ""

    _cache[key] = (stat.st_mtime, stat.st_size, text)
    return text


def append_voice(context: str, path: str | Path | None = None) -> str:
    """Append the voice block to a retrieved KB context.

    Unconditional by design: the candidate's motivation is relevant to every
    letter, whereas which *projects* are relevant depends on the job.
    """
    voice = load_voice(path)
    if not voice:
        return context
    return f"{context}{_SEPARATOR}{VOICE_HEADER}\n{voice}"
