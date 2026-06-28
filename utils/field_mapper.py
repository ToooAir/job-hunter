"""field_mapper.py — deterministic field-table → form_payload mapping (Step 4.1).

Pure module: no LLM, no browser, no DB. Consumes the Step 3 field node table
(FormField.to_dict() records) plus the candidate profile and produces the
form_payload parts of ApplyState:

  actions   — executable fill instructions for Step 5
              {selector, frame_path, kind, label, action, value, source,
               needs_review}; action ∈ fill|select_option|check|upload
  pending   — fields this mapper could not place; handed to the LLM mapper
              (4.2) with a `reason` and, when available, a profile-derived
              `suggestion` the LLM only has to confirm
  unfilled  — fields nothing can fill automatically (extra document slots);
              surfaced in review so they are never silently dropped
  never_fill_skipped — labels matched against the profile's never_fill list

The mapper never guesses: anything ambiguous goes to `pending`, and every
input field ends up in exactly one of the four buckets.
"""

from __future__ import annotations

import re
from datetime import date

from utils.profile_loader import CandidateProfile, ProfileField

# HTML autocomplete tokens are a standards-level gift: when present they
# identify the field more reliably than any label heuristic.
AUTOCOMPLETE_MAP = {
    "given-name": "first_name",
    "family-name": "last_name",
    "name": "full_name",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "street-address": "street_address",
    "address-line1": "street_address",
    "postal-code": "postal_code",
    "address-level2": "city",
    "country": "country",
    "country-name": "country",
    "bday": "date_of_birth",
    "url": "linkedin",
}

# Typed inputs whose kind alone identifies the profile field when the label
# matched nothing (e.g. an unlabeled type="email" input).
_KIND_FALLBACK = {"email": "email", "tel": "phone"}

# A matched profile key is rejected when the label clearly talks about
# something other than the candidate (profile_loader's documented
# mis-match risk: "company name" → full_name).
_NEGATIVE_GUARDS = {
    "full_name": ("company", "firma", "unternehmen", "school", "agentur"),
    "first_name": ("company", "firma", "unternehmen"),
    "last_name": ("company", "firma", "unternehmen"),
}

# Bidirectional synonyms for grounding profile values in select/radio options
# (German portals list German option labels; the profile stores one value).
_OPTION_SYNONYMS = {
    "germany": ("deutschland",),
    "yes": ("ja",),
    "no": ("nein",),
    "herr": ("mr", "mr."),
    "frau": ("ms", "ms.", "mrs", "mrs."),
    "männlich": ("male",),
    "weiblich": ("female",),
    "divers": ("diverse", "other"),
    "immediately": ("sofort", "ab sofort"),
}

_CV_FILE_RE = re.compile(r"\b(cv|lebenslauf|resume|résumé)\b", re.I)
_CL_RE = re.compile(
    r"anschreiben|cover\s*letter|motivationsschreiben|motivation\s*letter", re.I)
_GERMAN_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def _humanize(name: str) -> str:
    """'application[first_name]' → 'application first name' for alias matching."""
    return re.sub(r"[_\-.\[\]]+", " ", name or "").strip()


def _to_iso_date(value: str) -> str:
    """German DD.MM.YYYY → ISO for <input type=date>; anything else as-is."""
    m = _GERMAN_DATE_RE.match(value.strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else value


# "Bitte wählen" sits in the options list like any other entry, so an
# exact-match guard alone happily selects it (momox lesson) — placeholder
# choices must be rejected everywhere a value is decided.
_PLACEHOLDER_OPTION_RE = re.compile(
    r"^\s*(?:bitte\s+(?:aus)?wähle[nr]?|please\s+(?:select|choose)|"
    r"(?:select|choose)(?:\s+(?:one|an?\s+option))?|auswählen|auswahl|"
    r"keine\s+auswahl|-+|—+|–+|\.{2,})\s*[.…:]*\s*$", re.I)


def is_placeholder_option(text: str) -> bool:
    """True for non-choices like 'Bitte wählen' / 'Please select' / '--'."""
    return not (text or "").strip() or bool(_PLACEHOLDER_OPTION_RE.match(text))


def ground_option(value: str, options: list[str]) -> str | None:
    """The option that represents `value`, or None when nothing matches safely."""
    val = value.strip().casefold()
    if not val:
        return None
    options = [o for o in options if not is_placeholder_option(o)]
    by_fold = {opt.strip().casefold(): opt for opt in options}
    if val in by_fold:
        return by_fold[val]
    synonyms = set(_OPTION_SYNONYMS.get(val, ()))
    for fold, opt in by_fold.items():
        if fold in synonyms or val in _OPTION_SYNONYMS.get(fold, ()):
            return opt
    if len(val) >= 3:  # substring match either way, but never on tiny strings
        for fold, opt in by_fold.items():
            if val in fold or fold in val:
                return opt
    return None


def _match_profile_field(
    f: dict, profile: CandidateProfile,
) -> ProfileField | None:
    auto = (f.get("autocomplete") or "").split()
    for token in auto:
        key = AUTOCOMPLETE_MAP.get(token)
        if key and key in profile.fields:
            return profile.fields[key]

    label, name = f.get("label") or "", _humanize(f.get("name") or "")
    pf = profile.match_field(label) or (profile.match_field(name) if name else None)
    if pf is None and f.get("kind") in _KIND_FALLBACK:
        pf = profile.fields.get(_KIND_FALLBACK[f["kind"]])
    if pf is not None:
        guards = _NEGATIVE_GUARDS.get(pf.key, ())
        text = f"{label} {name}".casefold()
        if any(tok in text for tok in guards):
            return None
    return pf


def _action(f: dict, action: str, value: str, source: str,
            needs_review: bool = False) -> dict:
    # A positionally-guessed label (dom_pruner couldn't bind it to the control)
    # forces review no matter which mapping path produced the fill — the value
    # may be landing on the wrong field (watchlist #4, milia misalignment).
    out = {
        "selector": f.get("selector", ""),
        "kind": f.get("kind", ""),
        "label": f.get("label", ""),
        "action": action,
        "value": value,
        "source": source,
        "needs_review": needs_review or bool(f.get("label_suspect")),
    }
    if f.get("frame_path"):
        out["frame_path"] = f["frame_path"]
    return out


def _pending(f: dict, reason: str, suggestion: str | None = None,
             suggestion_source: str = "") -> dict:
    entry = {**f, "reason": reason}
    if suggestion is not None:
        entry["suggestion"] = suggestion
        entry["suggestion_source"] = suggestion_source
    return entry


def _unfilled(f: dict, reason: str) -> dict:
    return {"label": f.get("label", ""), "selector": f.get("selector", ""),
            "reason": reason, "required": bool(f.get("required"))}


def _map_file(f: dict, profile: CandidateProfile, result: dict) -> None:
    text = f"{f.get('label', '')} {_humanize(f.get('name') or '')}"
    if _CV_FILE_RE.search(text):
        # value is the profile's own (project-relative) path; the executor
        # resolves it against the repo root on whichever host it runs.
        result["actions"].append(_action(
            f, "upload", str(profile.meta.get("cv_path", "")), "profile:cv"))
    elif _CL_RE.search(text):
        # we hold the letter as text; rendering it to a file is Step 5/6 work
        result["unfilled"].append(_unfilled(f, "cover-letter-upload"))
    else:
        result["unfilled"].append(_unfilled(f, "attachment-unmapped"))


def _map_choice(f: dict, pf: ProfileField, result: dict) -> None:
    """select / radio: the profile value must ground in the offered options."""
    grounded = ground_option(pf.value, f.get("options") or [])
    if grounded is not None:
        result["actions"].append(_action(
            f, "select_option", grounded, f"profile:{pf.key}"))
    else:
        result["pending"].append(_pending(
            f, "option-not-grounded", pf.value, f"profile:{pf.key}"))


def map_fields(
    fields: list[dict],
    profile: CandidateProfile,
    today: date | None = None,
) -> dict:
    """Deterministic pass over a field node table. Every field lands in
    exactly one of: actions, pending, unfilled, never_fill_skipped."""
    result: dict = {
        "actions": [], "pending": [], "unfilled": [], "never_fill_skipped": [],
    }

    for f in fields:
        label = f.get("label") or ""
        name_h = _humanize(f.get("name") or "")
        kind = f.get("kind", "text")

        if profile.is_never_fill(label) or (name_h and profile.is_never_fill(name_h)):
            result["never_fill_skipped"].append(label or name_h)
            continue

        if kind == "file":
            _map_file(f, profile, result)
            continue

        if kind == "checkbox":
            if profile.is_auto_consent(label):
                result["actions"].append(_action(f, "check", "", "profile:consent"))
            else:
                result["pending"].append(_pending(f, "checkbox-unknown"))
            continue

        # the cover-letter slot is content work, not profile data (4.2 fills it)
        if kind in ("textarea", "text") and _CL_RE.search(f"{label} {name_h}"):
            result["pending"].append(_pending(f, "cover-letter-slot"))
            continue

        pf = _match_profile_field(f, profile)
        if pf is None:
            result["pending"].append(_pending(f, "no-deterministic-match"))
            continue

        if kind in ("select", "radio"):
            _map_choice(f, pf, result)
            continue

        if kind == "custom":
            # custom widgets (react-select & friends) have no options in the
            # table; filling them is interaction work — suggest, don't act
            result["pending"].append(_pending(
                f, "custom-widget", pf.value, f"profile:{pf.key}"))
            continue

        if kind == "date":
            value = pf.resolve_date(today) if pf.date_spec else _to_iso_date(pf.value)
            result["actions"].append(_action(f, "fill", value, f"profile:{pf.key}"))
            continue

        if kind == "number":
            raw = (str(pf.extra.get("value_eur_year"))
                   if pf.key == "salary_expectation" and pf.extra.get("value_eur_year")
                   else pf.value)
            if re.fullmatch(r"\d+([.,]\d+)?", raw.strip()):
                result["actions"].append(_action(f, "fill", raw.strip(), f"profile:{pf.key}"))
            else:
                result["pending"].append(_pending(
                    f, "non-numeric-value", pf.value, f"profile:{pf.key}"))
            continue

        # text / email / tel / textarea
        result["actions"].append(_action(f, "fill", pf.value, f"profile:{pf.key}"))

    return result
