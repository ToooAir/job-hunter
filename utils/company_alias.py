"""Brand / trade-name aliases for a job's employer, mined from its own JD.

Why this exists: ``/email-match`` nominates from a closed list that shows only
the stored LEGAL name (e.g. ``U-Glow GmbH``). A decision email frequently
identifies the employer by a brand or sender domain (``prelytics.eu``) that
never string-matches the legal name, so a genuine application goes unmatched —
even though the brand sits in our own JD ("Bei U-Glow bzw. prelytics® …"). We
mine that alias once at ingest (``utils.db.upsert_job``) and carry it on the
job row so ``/email-match`` (and the dedup / matcher paths) can reuse it.

Precision over recall: a missed alias only keeps the status quo, but a wrong
alias adds noise to the LLM's nomination list, so every rule is conservative
and generic / legal tokens are dropped.

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

# A brand phrase: 1–3 words, only the first may be lowercase (so a connective
# followed by a filler word like "the market leader" captures "the" alone,
# which the blacklist then drops).
_NAME = r"([A-Za-z0-9][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,2})"

# Connective markers that introduce an alternative employer name.
_CONNECTIVES = [
    re.compile(r"\bbzw\.?\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\btrading as\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\bformerly(?:\s+known\s+as)?\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\ba\.?k\.?a\.?\s+" + _NAME, re.IGNORECASE),
    re.compile(r"\balso known as\s+" + _NAME, re.IGNORECASE),
]
# A trademarked token: the single word immediately before ® or ™. Kept to one
# token on purpose — a name run would drift left and swallow the preceding word
# ("product Initech™" → "Initech", not "product Initech").
_TRADEMARK = re.compile(r"([A-Za-z0-9][\w&.'-]*)\s*[®™]")
# "Legal / Brand" — gated below on the left side matching the company.
_SLASH = re.compile(r"([A-Za-z0-9][\w&.'-]*)\s*/\s*" + _NAME)

# Generic / structural tokens that are never a distinctive brand (normalized,
# i.e. lowercased with non-alphanumerics stripped).
_GENERIC = {
    "gmbh", "ag", "se", "kg", "kgaa", "ug", "inc", "ltd", "limited", "llc",
    "plc", "co", "cokg", "group", "gruppe", "holding", "team", "company",
    "software", "solutions", "systems", "technologies", "technology",
    "digital", "media", "labs", "consulting", "services", "ventures",
    "the", "and", "or", "we", "our", "us", "you", "your", "career", "careers",
    "job", "jobs", "gmbhcokg", "gmbhcokg", "ohg", "mbh",
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
    for pat in _CONNECTIVES:
        raw += pat.findall(head)
    raw += _TRADEMARK.findall(head)
    for left, right in _SLASH.findall(head):
        # only trust a slash when its left side IS the company (avoids paths,
        # "and/or", unrelated pairs)
        lk = _squash(left)
        if lk and company_key and (lk == company_key
                                   or lk in company_key or company_key in lk):
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
        seen.add(key)
        aliases.append(alias)
        if len(aliases) >= _MAX_ALIASES:
            break
    return ", ".join(aliases)
