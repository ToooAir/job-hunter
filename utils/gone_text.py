"""gone_text.py — shared "this posting is over" wording detector.

One regex, two consumers (the geo_de.py pattern — split out so the browser
layer and the HTTP-only modules stop needing each other):

  * utils.browser._gone_signal matches it against the RENDERED page text
    (document.body.innerText) during Stage 1's headless probe.
  * soft_gone() matches it against a raw HTTP body in ats_scan and the
    draft-liveness sweep — the cheap GET stage, where a posting that answers
    200 but says "position filled" used to pass as live and die in the
    reviewer's hands (12 of 19 manual draft rejections, 2026-07-08 review).

Raw HTML needs the script/style blocks stripped first: live SPA pages ship
i18n bundles that contain every one of these phrases as translation strings.
"""

import re

# Wording that means the posting itself is gone (not just form-less).
GONE_TEXT_RE = re.compile(
    r"nicht\s+mehr\s+(?:verfügbar|aktiv|online|vakant)|bereits\s+besetzt|"
    r"stelle\s+(?:ist|wurde)\s+(?:besetzt|deaktiviert|geschlossen)|"
    r"(?:anzeige|stellenanzeige)\s+(?:ist\s+)?abgelaufen|"
    r"no\s+longer\s+(?:available|active|accepting)|"
    r"position\s+has\s+been\s+filled|job\s+(?:has\s+)?expired|"
    r"vacancy\s+(?:is\s+)?closed|this\s+job\s+is\s+no\s+longer|"
    r"page\s+not\s+found|seite\s+nicht\s+gefunden|"
    # jobg8 ad-network interstitial for a dead tracked link (englishjobs
    # snapshot #83, 2026-07-08): the job page is unrecoverable behind it.
    r"interest\s+in\s+the\s+previous\s+job", re.I)

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def soft_gone(html: str | None) -> str | None:
    """The matched gone-phrase when a raw HTTP body says the posting is over,
    else None. Only text a visitor would see counts — scripts, styles and
    comments are dropped so i18n bundles on live pages can't false-positive."""
    if not html:
        return None
    text = _TAG_RE.sub(" ", _SCRIPT_STYLE_RE.sub(" ", html))
    m = GONE_TEXT_RE.search(text)
    return m.group(0) if m else None
