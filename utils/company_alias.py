"""Brand / trade-name aliases for a job's employer, mined from its own JD.

Why this exists: ``/email-match`` nominates from a closed list that shows only
the stored LEGAL name (e.g. ``U-Glow GmbH``). A decision email frequently
identifies the employer by a brand or sender domain (``prelytics.eu``) that
never string-matches the legal name, so a genuine application goes unmatched —
even though the brand sits in our own JD ("Bei U-Glow bzw. prelytics® …"). We
mine that alias once at ingest (``utils.db.upsert_job``) and carry it on the
job row so ``/email-match`` (and the dedup / matcher paths) can reuse it.

Precision over recall: a missed alias only keeps the status quo, but a wrong
alias adds noise to the LLM's nomination list. Validating candidate rules
against real JDs killed every loose one (bare ® trademark, "X / Y", ungated
"formerly / trading as / a.k.a." — all fire on third-party products, tech
stacks, URLs, or vendor lists), leaving exactly one trusted marker: the German
"‹company› bzw. ‹brand›" anchored on the company. Deliberately narrow.

Self-contained on purpose — ``utils.apply_queue`` imports ``utils.db`` and
``utils.db`` imports this module, so importing ``apply_queue`` here would close
a cycle. The legal-suffix regex is duplicated (kept in sync with
``apply_queue.normalize_company``) rather than imported.
"""

import re

# Kept in sync with utils.apply_queue._LEGAL_SUFFIX_RE.
_LEGAL_SUFFIX_RE = re.compile(
    r"\s*(?:"
    r"gmbh\s*&\s*co\.?\s*kg|se\s*&\s*co\.?\s*kg|&\s*co\.?\s*kg|co\.?\s*kg"
    r"|gmbh|ag|se|kgaa|kg|inc\.?|ltd\.?|limited|llc|plc"
    r"|ug(?:\s*\(haftungsbeschränkt\))?"
    r")\s*$",
    re.IGNORECASE,
)

# Scan only the JD head — brand identity lives in the intro, and this bounds
# the cost of running the extractor over 38k rows during backfill.
_SCAN_CHARS = 1500
_MAX_ALIASES = 3

# A brand phrase: 1–3 words, only the first may be lowercase (so a filler word
# like "im Team" is captured as "im" alone, which the blacklist then drops).
_NAME = r"([A-Za-z0-9][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,2})"

# A single token, used as the left (company) side of a gated marker.
_TOK = r"([A-Za-z0-9][\w&.'-]*)"

# The single trusted marker: "‹company› bzw. ‹brand›". 'bzw.' (German
# beziehungsweise, "or / respectively") is the pattern that carried the real
# case ("U-Glow bzw. prelytics®"), and gating it on the company being on the
# LEFT is what makes it safe. Everything looser was tried and dropped after
# validating against real JDs (see tests):
#   • a bare ® / ™ trademark — JDs cite other firms' products (CD®, about™);
#   • "X / Y" — collides with tech stacks (CI/CD, REST/GraphQL) and URL paths
#     ('ci' even hid inside 'getspeCIalfasteners');
#   • ungated "trading as / formerly / a.k.a." — fire on third-party vendor
#     mentions, e.g. a storage reseller's "Everpure (formerly Pure Storage)".
# NB: no re.IGNORECASE — it would make _NAME's [A-Z0-9] continuation match
# lowercase too, letting the brand run drift into following prose ("Acme bzw.
# Acme GmbH is the market leader" → "Acme GmbH is"). The token/keyword parts are
# already case-insensitive via character classes.
_GATED = [
    re.compile(_TOK + r"\s+[Bb][Zz][Ww]\.?\s+" + _NAME),
]

# Generic / structural / stop-word tokens that are never a distinctive brand
# (normalized: lowercased, non-alphanumerics stripped). Includes German
# function words so "‹company› bzw. im Team" cannot leak "im".
_GENERIC = {
    # legal / structural
    "gmbh", "ag", "se", "kg", "kgaa", "ug", "inc", "ltd", "limited", "llc",
    "plc", "co", "cokg", "gmbhcokg", "ohg", "mbh", "group", "gruppe",
    "holding", "team", "company", "firma",
    # industry filler
    "software", "solutions", "systems", "technologies", "technology",
    "digital", "media", "labs", "consulting", "services", "ventures",
    # English stop words
    "the", "and", "or", "we", "our", "us", "you", "your", "career", "careers",
    "job", "jobs", "a", "an", "of", "for", "with",
    # German stop words / conjunction fillers
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "im", "in", "um", "am", "an", "auf", "bei", "mit", "für", "und", "oder",
    "auch", "als", "wir", "uns", "unsere", "unser", "sowie", "bzw",
}


def _squash(name: str) -> str:
    """Lowercase, drop a trailing legal suffix, strip non-alphanumerics — the
    matching key (mirrors apply_queue.normalize_company, minus the loop)."""
    norm = re.sub(r"\s+", " ", (name or "").strip().lower())
    prev = None
    while norm and norm != prev:
        prev = norm
        norm = _LEGAL_SUFFIX_RE.sub("", norm).rstrip(" ,.-")
    return "".join(c for c in norm if c.isalnum())


def _company_match(left_key: str, company_key: str) -> bool:
    """Is ``left_key`` (a marker's left side) the company? Exact, or a
    containment where the shorter side is ≥4 chars (so a 2-char token can't
    match inside an unrelated name)."""
    if not left_key or not company_key:
        return False
    if left_key == company_key:
        return True
    shorter = min(left_key, company_key, key=len)
    return len(shorter) >= 4 and (left_key in company_key
                                  or company_key in left_key)


def _display(candidate: str) -> str:
    """Trim a raw match to a clean display alias (legal suffix / punctuation
    stripped) while preserving its original casing."""
    text = re.sub(r"\s+", " ", (candidate or "").strip()).strip(" ,.®™'\"-")
    stripped = _LEGAL_SUFFIX_RE.sub("", text).rstrip(" ,.-")
    return stripped or text


def extract_company_aliases(company: str, jd_text: str) -> str:
    """Mine up to a few brand / trade-name aliases for ``company`` from its JD.

    Returns a comma-joined string (possibly empty). Each alias is distinct from
    the legal name and from every generic / structural token.
    """
    if not jd_text or not company:
        return ""
    head = jd_text[:_SCAN_CHARS]
    company_key = _squash(company)

    raw: list[str] = []
    for pat in _GATED:
        for left, right in pat.findall(head):
            # trust the marker only when its left side IS the company. Substring
            # is allowed (multi-word names collapse to one token) but only when
            # the shorter side is ≥4 chars — else a 2-char token like 'ci' spuriously
            # matches inside 'getspeCIalfasteners'.
            if _company_match(_squash(left), company_key):
                raw.append(right)

    aliases: list[str] = []
    seen: set[str] = set()
    for cand in raw:
        alias = _display(cand)
        key = _squash(alias)
        if not key or len(alias) < 2 or len(alias) > 40:
            continue
        if key == company_key or key in _GENERIC or key in seen:
            continue
        # a brand never opens with a function word ("im Team", "the group")
        words = alias.split()
        if words and _squash(words[0]) in _GENERIC:
            continue
        seen.add(key)
        aliases.append(alias)
        if len(aliases) >= _MAX_ALIASES:
            break
    return ", ".join(aliases)
