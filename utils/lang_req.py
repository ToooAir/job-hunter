"""utils/lang_req.py — rule-level detection of a HARD German-language requirement.

Why this exists (2026-09-02)
----------------------------
57% of the scored German pool is jd_language_req='de_required', and every one
of those rows is grade C by construction (see ScoringResult.derive_grade). The
scorer still paid for each of them twice: a translation call (German JDs are
translated before scoring) and the scoring call itself. The requirement is
almost always stated in a handful of fixed phrases, so a regex catches the bulk
before any LLM spend.

Measured on 6,146 scored rows before this gate: the German-anchored pattern
below hit 1,499 of 2,546 de_required rows and 75 of the 5,000 rows the LLM
labelled otherwise — and most of those 75 were LLM mislabels ("verhandlungs-
sichere Deutsch- und gute Englischkenntnisse" filed as de_plus). The obvious
trap is `\\bC1\\b` on its own: "English C1" and "#C1" hit 23 A/B rows. Every
branch is therefore anchored on deutsch/german, an English/Englisch mention
inside the matched span disqualifies it (the phrase is then about English), and
a softener within ±60 characters ("von Vorteil", "nice to have", "or German")
means the JD is negotiable and the LLM keeps the call.

The gate is deliberately conservative: a miss costs one LLM call, a false
positive hides a job the candidate could have applied to.
"""

from __future__ import annotations

import re

_LEVEL_DE = r"(?:\bc1\b|\bc2\b|verhandlungssicher\w*|flie(?:ß|ss)end\w*|sehr gute?\w*|muttersprach\w*)"
# no bare "proficiency"/"professional": "German language proficiency (B1 or
# above)" is not a hard requirement, and the level lives outside the span
_LEVEL_EN = r"(?:\bc1\b|\bc2\b|fluent|fluency|native|business[- ]level)"

GERMAN_REQUIRED_RE = re.compile(
    # "Deutschkenntnisse auf C1-Niveau", "Deutsch fließend", "sehr gute Deutschkenntnisse"
    rf"deutsch\w*[^.\n;,]{{0,40}}{_LEVEL_DE}"
    rf"|{_LEVEL_DE}[^.\n;,]{{0,40}}deutsch\w*"
    # "German C1", "fluent German", "German at native level" — \bgerman\b so
    # "based in Germany" ("native ... Germany") cannot match
    rf"|\bgerman\b[^.\n;,]{{0,30}}{_LEVEL_EN}"
    rf"|{_LEVEL_EN}[^.\n;,]{{0,30}}\bgerman\b",
    re.I,
)

# The phrase is about English, not German ("sehr gute Englisch- und gute
# Deutschkenntnisse"; "fluent in English, German B1").
_OTHER_LANG_RE = re.compile(r"englis[hc]h?|english", re.I)

# Negotiable / optional / either-language wording within the window.
SOFTENER_RE = re.compile(
    r"von vorteil|nice[- ]to[- ]have|\bplus\b|wünschenswert|wuenschenswert|"
    r"preferred|bonus|ideal(?:ly|erweise)|advantage|desirable|"
    r"not required|nicht erforderlich|not mandatory|optional|"
    r"or german|oder deutsch|german or\b|deutsch oder|english or\b|englisch oder|"
    r"willing to learn|bereitschaft.{0,20}lernen",
    re.I,
)

WINDOW = 60


def german_required(text: str | None) -> str | None:
    """Return the matched phrase when the JD states a hard German requirement,
    else None. Conservative on purpose — see module docstring."""
    if not text:
        return None
    for m in GERMAN_REQUIRED_RE.finditer(text):
        span = m.group(0)
        if _OTHER_LANG_RE.search(span):
            continue
        window = text[max(0, m.start() - WINDOW): m.end() + WINDOW]
        if SOFTENER_RE.search(window):
            continue
        return " ".join(span.split())
    return None
