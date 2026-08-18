"""apply_url.py — is this URL something a human could actually apply on?

Shared by ats_scan (which mines apply links out of raw HTML, where an ATS
domain match can just as easily be a script src or a footer link) and by
phase1's bundesagentur scraper (whose externeURL field is a homepage or a
self-reference two times out of three). Both need the same question answered
and neither should have to import the other: ats_scan pulls in requests/bs4,
phase1 must stay light.

Pure stdlib.
"""

import re
from urllib.parse import urlparse

# scan_text_for_ats matches ATS domains anywhere in raw HTML, so evidence
# can be a script src (…successfactors.eu/…/jquery.js) or a footer link
# (join.com/terms). Such evidence still proves WHICH ats hosts the job,
# but must never be stored as the apply link.
_STATIC_ASSET_EXTS = (".js", ".css", ".map", ".json", ".png", ".jpg", ".jpeg",
                      ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf")
# …and the extensionless form: Greenhouse serves its board loader as the PATH
# /embed/job_board/js, which endswith(".js") never catches.
_ASSET_TAIL_RE = re.compile(r"/(js|css)$", re.I)
_JUNK_PATH_RE = re.compile(
    r"/(terms|privacy(-policy)?|legal|imprint|impressum|datenschutz|agb|"
    r"cookies?|cookie-richtlinie)(/|$|\?)", re.I)
_LOCALE_ONLY_PATH_RE = re.compile(r"^/?[a-z]{2}([_-][a-z]{2})?/?$", re.I)
# A bare path is only a homepage when nothing else identifies the posting:
# compleet serves real deep links as jobboard.compleet.com/?externalId=<id>.
# Tracking parameters do not count — they identify the referrer, not the job.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "ref", "referrer", "source", "src", "lang",
})


def plausible_apply_url(url):
    """True when the URL could be a real apply page — rejects static assets,
    terms/privacy pages, and bare (or locale-only) homepages."""
    u = (url or "").strip()
    if u.startswith("mailto:"):
        return True
    if not u.startswith(("http://", "https://")):
        return False
    parts = urlparse(u)
    path = parts.path
    if path in ("", "/") or _LOCALE_ONLY_PATH_RE.match(path):
        keys = {k.split("=", 1)[0].lower() for k in parts.query.split("&") if k}
        if not (keys - _TRACKING_PARAMS):
            return False
    if path.rstrip("/").lower().endswith(_STATIC_ASSET_EXTS):
        return False
    if _ASSET_TAIL_RE.search(path.rstrip("/")):
        return False
    if _JUNK_PATH_RE.search(path):
        return False
    return True
