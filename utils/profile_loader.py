"""profile_loader.py — load candidate_profile.yaml and match form fields to it.

The apply agent maps standard form fields deterministically (no LLM call):
`match_field("Vorname *")` returns the profile field whose alias list best
matches the label. Open-ended questions that match nothing fall through to
the LLM+RAG path instead.

Matching rules:
- labels and aliases are normalized (lowercase, punctuation stripped)
- an alias must appear as a whole-word phrase in the label, so "land"
  (country) does not match inside "Deutschland"
- when several fields match, the longest alias wins — "first name" beats
  the generic "name" alias of full_name

Known limitation: short generic aliases can still mis-match unusual labels
(e.g. "company name" → full_name). The Stage-1 verifier is the safety net;
add negative guards in the mapper if a pattern shows up in practice.

See candidate_kb/candidate_profile.yaml.example for the file format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE_PATH = PROJECT_ROOT / "candidate_kb" / "candidate_profile.yaml"

_DATE_SPEC_RE = re.compile(r"^\+(\d+)\s*days?$", re.I)


class ProfileError(ValueError):
    """Profile file missing or malformed."""


class ProfileIncompleteError(ProfileError):
    """Profile still contains TODO placeholders — refuse to fill forms with it."""


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation so labels like 'E-Mail *:' match aliases."""
    text = text.lower()
    text = re.sub(r"[*:()\[\]{}\"'?!,;.]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_phrase(label_norm: str, alias_norm: str) -> bool:
    """Whole-word phrase match: alias must not sit inside a longer word."""
    return re.search(rf"\b{re.escape(alias_norm)}\b", label_norm) is not None


@dataclass(frozen=True)
class ProfileField:
    key: str
    value: str
    aliases: tuple[str, ...]
    date_spec: str | None = None
    explanation: str | None = None
    # Synonyms the VALUE may appear as in a <select>'s real options (e.g.
    # value "Germany" but the dropdown says "Deutschland") — the label
    # aliases above answer "is this field mine?", these answer "which
    # option is my value?".
    option_aliases: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)  # e.g. value_eur_year

    def resolve_date(self, today: date | None = None) -> str | None:
        """Concrete ISO date for date-picker fields.

        date_value "+N days" is computed at fill time; any other string is
        returned as-is (assumed to be a concrete date already).
        """
        if not self.date_spec:
            return None
        m = _DATE_SPEC_RE.match(self.date_spec.strip())
        if m:
            base = today or date.today()
            return (base + timedelta(days=int(m.group(1)))).isoformat()
        return self.date_spec


class CandidateProfile:
    def __init__(self, data: dict):
        if not isinstance(data, dict) or "fields" not in data:
            raise ProfileError("profile yaml must be a mapping with a 'fields' section")

        self.meta: dict = data.get("meta") or {}
        self.fields: dict[str, ProfileField] = {}
        for key, spec in (data.get("fields") or {}).items():
            if not isinstance(spec, dict) or "value" not in spec:
                raise ProfileError(f"field '{key}' needs at least a 'value' entry")
            aliases = tuple(_normalize(str(a)) for a in spec.get("aliases", []))
            extra = {
                k: v for k, v in spec.items()
                if k not in ("value", "aliases", "option_aliases",
                             "date_value", "explanation")
            }
            self.fields[key] = ProfileField(
                key=key,
                value=str(spec["value"]),
                aliases=aliases,
                date_spec=spec.get("date_value"),
                explanation=spec.get("explanation"),
                option_aliases=tuple(str(o) for o in spec.get("option_aliases", [])),
                extra=extra,
            )

        consents = data.get("consents") or {}
        self._consent_aliases = [
            _normalize(str(a)) for a in consents.get("auto_accept_aliases", [])
        ]
        # never_fill entries are "english label / german label" strings
        self._never_fill_tokens = [
            _normalize(tok)
            for entry in (data.get("never_fill") or [])
            for tok in str(entry).split("/")
            if tok.strip()
        ]

    # ── form-field matching ────────────────────────────────────────────────

    def match_field(self, label: str) -> ProfileField | None:
        """Best profile field for a form label/name/id, or None (→ LLM path)."""
        label_norm = _normalize(label)
        if not label_norm:
            return None
        best: ProfileField | None = None
        best_len = 0
        for f in self.fields.values():
            for alias in f.aliases:
                if len(alias) > best_len and _contains_phrase(label_norm, alias):
                    best, best_len = f, len(alias)
        return best

    def is_never_fill(self, label: str) -> bool:
        """True for fields the agent must leave blank and flag for review."""
        label_norm = _normalize(label)
        return any(_contains_phrase(label_norm, tok) for tok in self._never_fill_tokens)

    def is_auto_consent(self, label: str) -> bool:
        """True for consent checkboxes the agent may tick automatically."""
        label_norm = _normalize(label)
        return any(_contains_phrase(label_norm, a) for a in self._consent_aliases)

    # ── meta accessors ─────────────────────────────────────────────────────

    @property
    def cl_language(self) -> str:
        return str(self.meta.get("cl_language", "en"))

    @property
    def cv_path(self) -> Path:
        """CV path resolved against the project root when relative."""
        raw = Path(str(self.meta.get("cv_path", "")))
        return raw if raw.is_absolute() else PROJECT_ROOT / raw

    # ── completeness check ─────────────────────────────────────────────────

    def todo_residue(self, check_cv_file: bool = True) -> list[str]:
        """Entries the user still has to fill before the profile is usable."""
        todos: list[str] = []
        for key, f in self.fields.items():
            if "TODO" in f.value:
                todos.append(f"fields.{key}.value")
            for ek, ev in f.extra.items():
                if isinstance(ev, str) and "TODO" in ev:
                    todos.append(f"fields.{key}.{ek}")
        # salary needs a real number for numeric-only form inputs
        salary = self.fields.get("salary_expectation")
        if salary and not salary.extra.get("value_eur_year"):
            todos.append("fields.salary_expectation.value_eur_year")
        if "TODO" in str(self.meta.get("cv_path", "")):
            todos.append("meta.cv_path")
        elif check_cv_file and not self.cv_path.is_file():
            todos.append(f"meta.cv_path (file not found: {self.cv_path})")
        return todos


def load_profile(
    path: str | Path | None = None,
    strict: bool = True,
    check_cv_file: bool = True,
) -> CandidateProfile:
    """Load and validate the candidate profile.

    strict=True (the apply agent's mode) refuses profiles with TODO residue
    or a missing CV file, so a half-filled profile can never reach a form.
    """
    p = Path(path) if path else DEFAULT_PROFILE_PATH
    if not p.is_file():
        raise ProfileError(
            f"profile not found: {p} — copy candidate_profile.yaml.example "
            "to candidate_profile.yaml and fill in the TODOs"
        )
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"invalid yaml in {p}: {exc}") from exc

    profile = CandidateProfile(data)
    if strict:
        todos = profile.todo_residue(check_cv_file=check_cv_file)
        if todos:
            raise ProfileIncompleteError(
                f"profile {p} is incomplete — fill these before applying: "
                + ", ".join(todos)
            )
    return profile
