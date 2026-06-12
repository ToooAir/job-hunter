"""dom_pruner.py — turn a job-application page into an LLM-sized field table.

Core contract of the browser layer (Step 3): the output is NOT prose HTML but
a *field node table*. Every fillable control gets
  * a stable CSS selector (plus the iframe path the browser layer prepends),
    so Step 4 can map an LLM answer back onto the real element, and
  * the surrounding semantics (label / aria / placeholder / name) that the
    LLM uses to decide WHAT the field means.

Pure offline module: BeautifulSoup only, no Playwright dependency, so it is
unit-testable everywhere (host and container).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from bs4 import BeautifulSoup, Comment, Tag

PRUNE_BUDGET_BYTES = 15_000

# Elements that never contain form semantics.
_STRIP_TAGS = ("script", "style", "svg", "noscript", "template", "link", "meta")
# Page chrome stripped when it does not contain the form.
_CHROME_TAGS = ("nav", "header", "footer", "aside")

_SKIP_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset",
                     "search", "password"}

# Containers whose controls are page chrome, never application fields
# (cookie-consent managers, search bars). Matched against id/class of any
# ancestor; nav/header/footer/aside ancestors disqualify too.
_NOISE_CONTAINER_RE = re.compile(
    r"cookie|consent|usercentrics|onetrust|didomi|cookiebot|cmp-|gdpr", re.I)
_SEARCH_FORM_RE = re.compile(r"search|suche", re.I)

# Attributes worth keeping on pruned HTML / reporting in the field table.
_SEMANTIC_ATTRS = (
    "name", "id", "type", "placeholder", "aria-label", "aria-labelledby",
    "aria-required", "required", "autocomplete", "value", "for", "role",
    "maxlength", "accept", "multiple",
)

_CUSTOM_WIDGET_ROLES = {"combobox", "listbox", "spinbutton"}

_MAX_OPTIONS = 25
_MAX_LABEL_LEN = 160


@dataclass
class FormField:
    selector: str                       # CSS selector, unique within its document
    frame_path: tuple[str, ...] = ()    # iframe selectors from top document inward
    kind: str = "text"                  # text|email|tel|number|date|textarea|select|
                                        # checkbox|radio|file|custom
    label: str = ""
    name: str = ""
    options: list[str] = field(default_factory=list)
    required: bool = False
    autocomplete: str = ""
    accept: str = ""                    # file inputs: accepted MIME/extensions

    def to_dict(self) -> dict:
        d = {"selector": self.selector, "kind": self.kind, "label": self.label}
        if self.frame_path:
            d["frame_path"] = list(self.frame_path)
        if self.name:
            d["name"] = self.name
        if self.options:
            d["options"] = self.options
        if self.required:
            d["required"] = True
        if self.autocomplete:
            d["autocomplete"] = self.autocomplete
        if self.accept:
            d["accept"] = self.accept
        return d


def _css_escape(value: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_ -￿-])", r"\\\1", value)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:_MAX_LABEL_LEN]


def _selector_for(el: Tag, soup: BeautifulSoup) -> str:
    """Stable selector: #id → tag[name=] (+[value=] for radios) → nth-of-type path."""
    el_id = el.get("id")
    if el_id:
        # ids starting with a digit (UUIDs — Personio does this) are invalid
        # in #-notation; attribute form works for anything
        sel = (f"#{_css_escape(el_id)}" if re.match(r"^[A-Za-z_]", el_id)
               else f'[id="{el_id}"]')
        try:
            if len(soup.select(sel)) == 1:
                return sel
        except Exception:
            pass
    name = el.get("name")
    if name:
        sel = f'{el.name}[name="{name}"]'
        if el.get("type") == "radio" and el.get("value") is not None:
            sel += f'[value="{el["value"]}"]'
        if len(soup.select(sel)) == 1:
            return sel
    # Positional fallback: path of tag:nth-of-type from the document root.
    parts = []
    node = el
    while isinstance(node, Tag) and node.name not in ("html", "[document]"):
        siblings = [s for s in node.parent.find_all(node.name, recursive=False)] if node.parent else [node]
        idx = siblings.index(node) + 1
        parts.append(f"{node.name}:nth-of-type({idx})")
        node = node.parent
    return " > ".join(reversed(parts))


def _label_for(el: Tag, soup: BeautifulSoup) -> str:
    """Best-effort human label, in priority order."""
    el_id = el.get("id")
    if el_id:
        lab = soup.find("label", attrs={"for": el_id})
        if lab:
            return _clean_text(lab.get_text(" "))
    wrapping = el.find_parent("label")
    if wrapping:
        return _clean_text(wrapping.get_text(" "))
    if el.get("aria-label"):
        return _clean_text(el["aria-label"])
    labelledby = el.get("aria-labelledby")
    if labelledby:
        texts = []
        for ref in labelledby.split():
            target = soup.find(id=ref)
            if target:
                texts.append(target.get_text(" "))
        if texts:
            return _clean_text(" ".join(texts))
    if el.get("placeholder"):
        return _clean_text(el["placeholder"])
    # Nearby text: previous sibling label-ish element inside the same container.
    prev = el.find_previous_sibling(("label", "span", "div", "p"))
    if prev is not None:
        text = _clean_text(prev.get_text(" "))
        if 0 < len(text) <= 80:
            return text
    return _clean_text((el.get("name") or "").replace("_", " ").replace("-", " "))


def _is_required(el: Tag, label: str) -> bool:
    if el.has_attr("required") or el.get("aria-required") == "true":
        return True
    return label.rstrip().endswith("*")


def _input_kind(el: Tag) -> str | None:
    """Field kind, or None if the element should be skipped."""
    if el.name == "textarea":
        return "textarea"
    if el.name == "select":
        return "select"
    if el.name == "input":
        itype = (el.get("type") or "text").lower()
        if itype in _SKIP_INPUT_TYPES:
            return None
        if itype in ("checkbox", "radio", "file", "email", "tel", "number", "date", "url"):
            return itype
        return "text"
    if el.get("role") in _CUSTOM_WIDGET_ROLES or el.get("contenteditable") == "true":
        return "custom"
    return None


def _is_noise(el: Tag) -> bool:
    """True for page-chrome controls: anything inside nav/header/footer/aside,
    a cookie-consent container, or a site-search form."""
    node = el
    while isinstance(node, Tag):
        if node.name in _CHROME_TAGS or node.get("role") == "search":
            return True
        if node.name == "form" and _SEARCH_FORM_RE.search(
                f"{node.get('action') or ''} {node.get('id') or ''}"):
            return True
        idcls = " ".join([node.get("id") or ""] + (node.get("class") or []))
        if idcls.strip() and _NOISE_CONTAINER_RE.search(idcls):
            return True
        node = node.parent
    return False


def _application_form(soup: BeautifulSoup) -> Tag | None:
    """The <form> that plausibly IS the application form, or None.

    Signature: a file upload, or at least two text-entry controls. Login
    forms (password input), search forms, and consent containers never
    qualify — board pages are full of those (Step 3 probe finding).
    """
    best, best_score = None, 0
    for form in soup.find_all("form"):
        if form.find("input", attrs={"type": "password"}) or _is_noise(form):
            continue
        textish = sum(
            1 for c in form.find_all(("input", "textarea"))
            if _input_kind(c) in ("text", "email", "tel", "url", "number",
                                  "date", "textarea")
        )
        files = len(form.find_all("input", attrs={"type": "file"}))
        if files == 0 and textish < 2:
            continue
        score = textish + 3 * files
        if score > best_score:
            best, best_score = form, score
    return best


def extract_fields(
    html: str,
    frame_path: tuple[str, ...] = (),
    scope_to_form: bool = True,
) -> list[FormField]:
    """Field node table for one document. Radio groups collapse to one field.

    With scope_to_form (the default) extraction is confined to the detected
    application <form>; on form-less SPA markup it falls back to the whole
    document minus page chrome (search bars, cookie consents, nav).
    """
    soup = BeautifulSoup(html, "html.parser")
    scope = (_application_form(soup) if scope_to_form else None) or soup
    fields: list[FormField] = []
    seen_radio_groups: set[str] = set()

    candidates = scope.find_all(("input", "textarea", "select")) + [
        el for el in scope.find_all(attrs={"role": True})
        if el.get("role") in _CUSTOM_WIDGET_ROLES and el.name not in ("input", "textarea", "select")
    ]
    for el in candidates:
        kind = _input_kind(el)
        if kind is None:
            continue
        if scope is soup and scope_to_form and _is_noise(el):
            continue

        if kind == "radio":
            group = el.get("name") or ""
            if group in seen_radio_groups:
                continue
            seen_radio_groups.add(group)
            radios = scope.find_all("input", attrs={"type": "radio", "name": group}) if group else [el]
            options = [_label_for(r, soup) or (r.get("value") or "") for r in radios]
            group_label = _group_label(radios[0], soup) or group.replace("_", " ")
            fields.append(FormField(
                selector=_selector_for(el, soup), frame_path=frame_path, kind="radio",
                label=_clean_text(group_label), name=group,
                options=[o for o in options if o][:_MAX_OPTIONS],
                required=any(_is_required(r, "") for r in radios),
            ))
            continue

        label = _label_for(el, soup)
        f = FormField(
            selector=_selector_for(el, soup), frame_path=frame_path, kind=kind,
            label=label, name=el.get("name") or "",
            required=_is_required(el, label),
            autocomplete=el.get("autocomplete") or "",
        )
        if kind == "select":
            f.options = [
                _clean_text(o.get_text(" ")) for o in el.find_all("option")
                if _clean_text(o.get_text(" "))
            ][:_MAX_OPTIONS]
        if kind == "file":
            f.accept = el.get("accept") or ""
        fields.append(f)
    return fields


def _group_label(radio: Tag, soup: BeautifulSoup) -> str:
    """Label of a radio group: fieldset legend, else container's leading text."""
    fieldset = radio.find_parent("fieldset")
    if fieldset:
        legend = fieldset.find("legend")
        if legend:
            return legend.get_text(" ")
    container = radio.find_parent(("div", "section"))
    if container:
        first = container.find(("label", "span", "p", "legend"))
        if first and not first.find(("input",)):
            return first.get_text(" ")
    return ""


def _form_root(soup: BeautifulSoup) -> Tag:
    """The subtree that holds the application form.

    Prefer the <form> with the most fillable controls; with no <form> at all
    (SPA markup), fall back to the smallest container holding all controls.
    """
    forms = soup.find_all("form")
    scored = [
        (len(f.find_all(("input", "textarea", "select"))), f) for f in forms
    ]
    scored = [(n, f) for n, f in scored if n > 0]
    if scored:
        return max(scored, key=lambda t: t[0])[1]
    controls = soup.find_all(("input", "textarea", "select"))
    if not controls:
        return soup.body or soup
    node = controls[0]
    while node.parent is not None and node.name != "body":
        if all(c in node.descendants for c in controls):
            return node
        node = node.parent
    return soup.body or soup


def prune_html(html: str, budget: int = PRUNE_BUDGET_BYTES) -> str:
    """Slimmed HTML of the form subtree, for LLM schema extraction context.

    Keeps structure and semantic attributes only; drops scripts, styling,
    page chrome, comments, and non-semantic attributes (class/style/data-*).
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    root = _form_root(soup)
    for tag in root.find_all(_CHROME_TAGS):
        tag.decompose()
    for el in [root] + root.find_all(True):
        el.attrs = {k: v for k, v in el.attrs.items() if k in _SEMANTIC_ATTRS}

    out = str(root)
    out = re.sub(r"\n\s*\n+", "\n", out)
    if len(out.encode()) > budget:
        # Last resort: drop free text between fields, keep the control skeleton.
        for el in root.find_all(("p", "ul", "ol")):
            if not el.find(("input", "textarea", "select", "label")):
                el.decompose()
        out = re.sub(r"\n\s*\n+", "\n", str(root))
    return out
