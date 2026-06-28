"""browser.py — shared browser layer for the apply pipeline (Step 3).

headless_session(): persistent-profile headless Chromium for Stage 1
(unattended schema extraction + draft generation). Runs inside the pipeline
container; the profile lives on the mounted data volume so cookie consent
sticks. This is the only browser the system drives — there is no live
submission browser: applications are generated here and the human copies the
answer sheet onto the real form themselves.

Policy: cookie walls are answered with "necessary only", never "accept all".
Captchas are detected and reported, never bypassed.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from utils.dom_pruner import FormField, extract_fields, prune_html

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = Path(os.getenv("BROWSER_PROFILE_DIR", str(ROOT / "data" / "browser_profile")))

NAV_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 8_000
CLICK_TIMEOUT_MS = 1_200
FRAME_OP_TIMEOUT_MS = 3_000  # per-frame evaluate/content cap — ad/tracker
                             # iframes may never get an execution context

# Ordered decline-first patterns (user decision 2026-06-11: necessary only,
# never "Alle akzeptieren"). Most specific first.
COOKIE_DECLINE_PATTERNS = [
    r"nur\s+(?:technisch\s+)?notwendige",
    r"nur\s+erforderliche",
    r"notwendige\s+(?:cookies\s+)?(?:akzeptieren|zulassen|erlauben)",
    r"alle\s+ablehnen",
    r"^\s*ablehnen\s*$",
    r"reject\s+all",
    r"decline\s+all",
    r"only\s+(?:strictly\s+)?necessary",
    r"necessary\s+(?:cookies\s+)?only",
    r"essential\s+only",
    r"^\s*decline\s*$",
    r"weiter\s+ohne\s+(?:einwilligung|zustimmung)",
]

APPLY_BUTTON_PATTERNS = [
    r"jetzt\s+(?:online\s+)?bewerben",
    r"online\s*[- ]?bewerbung",
    r"bewerben",
    r"apply\s+(?:now|online|for\s+this)",
    r"^\s*apply\s*$",
    r"zur\s+bewerbung",
]

_CAPTCHA_MARKERS = ("recaptcha", "hcaptcha", "turnstile", "cf-challenge", "geetest")

# Fallback for JS apply buttons whose popup gets blocked (async window.open
# loses the user-gesture token): the destination usually also exists in the
# DOM as a plain anchor. Prefer off-site hrefs — that's where apply flows go.
_APPLY_HREF_JS = """
() => {
  const cand = [...document.querySelectorAll('a[href]')].filter(a => {
    const raw = a.getAttribute('href') || '';
    if (/^(javascript:|#|mailto:)/i.test(raw)) return false;
    return /apply|bewerb/i.test((a.innerText || '') + ' ' + raw);
  });
  const ext = cand.find(a => a.host && a.host !== location.host);
  return (ext || cand[0]) ? (ext || cand[0]).href : null;
}
"""

# Counted by control class: an application form is recognised by text-entry
# fields and uploads, NOT by checkbox/search-box noise (board pages are full
# of "copy job id" checkboxes and search inputs).
_COUNT_CONTROLS_JS = """
() => {
  const T = 'input[type=text],input[type=email],input[type=tel],input[type=url],input:not([type]),textarea';
  const F = 'input[type=file]';
  const S = 'select';
  const CR = 'input[type=checkbox],input[type=radio]';
  const res = {textish: 0, file: 0, select: 0, checkbox_radio: 0, shadow: 0,
               password: 0};
  const add = (root) => {
    res.textish += root.querySelectorAll(T).length;
    res.file += root.querySelectorAll(F).length;
    res.select += root.querySelectorAll(S).length;
    res.checkbox_radio += root.querySelectorAll(CR).length;
    res.password += root.querySelectorAll('input[type=password]').length;
  };
  add(document);
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        res.shadow += el.shadowRoot.querySelectorAll(T + ',' + F + ',' + S).length;
        walk(el.shadowRoot);
      }
    }
  };
  walk(document);
  return res;
}
"""


class ProfileBusyError(RuntimeError):
    """The persistent browser profile is in use by another process."""


@contextmanager
def profile_lock(profile_dir: Path):
    """Single-flight lock so scheduler and manual runs never share a profile."""
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_path = profile_dir / ".apply-agent.lock"
    if lock_path.exists():
        try:
            pid = int(lock_path.read_text().strip() or 0)
            os.kill(pid, 0)  # raises if the holder is gone
            raise ProfileBusyError(f"profile {profile_dir} locked by pid {pid}")
        except (ValueError, ProcessLookupError, PermissionError):
            lock_path.unlink(missing_ok=True)  # stale lock — steal it
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


@contextmanager
def headless_session(profile_dir: Path | None = None, headless: bool = True):
    """Persistent-profile Chromium context for Stage 1 (read-only prep work)."""
    profile_dir = Path(profile_dir or PROFILE_DIR)
    with profile_lock(profile_dir):
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                locale="de-DE",
                timezone_id="Europe/Berlin",
                viewport={"width": 1366, "height": 900},
            )
            context.set_default_timeout(NAV_TIMEOUT_MS)
            try:
                yield context
            finally:
                context.close()


def dismiss_cookie_banner(page) -> str | None:
    """Click the 'necessary only' consent option. Returns clicked text or None.

    get_by_role pierces open shadow DOM (Usercentrics et al.); consent iframes
    (Cookiebot) are searched too. Best effort: a missed banner shows up later
    as form_found=False in the probe report, not as a crash.
    """
    scopes = list(_interesting_frames(page))
    for pattern in COOKIE_DECLINE_PATTERNS:
        rx = re.compile(pattern, re.IGNORECASE)
        for scope in scopes:
            for finder in (
                lambda s: s.get_by_role("button", name=rx),
                lambda s: s.locator("button, a, [role=button]").filter(has_text=rx),
            ):
                try:
                    loc = finder(scope).first
                    if loc.is_visible(timeout=CLICK_TIMEOUT_MS):
                        text = (loc.inner_text(timeout=CLICK_TIMEOUT_MS) or "").strip()
                        loc.click(timeout=CLICK_TIMEOUT_MS)
                        page.wait_for_timeout(400)
                        return text or pattern
                except Exception:
                    continue
    return None


def _settle(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
    except PlaywrightTimeout:
        pass  # SPAs with long-poll/analytics never go idle — proceed anyway


@contextmanager
def _frame_op_guard(page, ms: int = FRAME_OP_TIMEOUT_MS):
    """Cap per-frame operations so a stuck third-party iframe can't eat the
    default 30s timeout once per frame (news-ish pages embed a dozen)."""
    page.context.set_default_timeout(ms)
    try:
        yield
    finally:
        page.context.set_default_timeout(NAV_TIMEOUT_MS)


_SKIP_FRAME_HOSTS = ("doubleclick", "googletagmanager", "google-analytics",
                     "facebook.", "consent.", "adsystem", "adservice")


def _skip_frame(url: str) -> bool:
    """A child-frame URL we must not extract from: ad/analytics trackers, or a
    captcha frame. A captcha iframe hosts its own controls (language pickers,
    challenge inputs) — those must never leak into the field table or be filled
    (red line). Captcha is still DETECTED separately via detect_captcha."""
    u = (url or "").lower()
    return (any(h in u for h in _SKIP_FRAME_HOSTS)
            or any(m in u for m in _CAPTCHA_MARKERS))


def _interesting_frames(page):
    """Main frame plus child frames that could plausibly host a form.

    evaluate()/content() have NO timeout parameter and block until the frame
    has an execution context — a stuck tracker iframe blocks forever. So:
    filter known ad/analytics/captcha hosts, then probe readiness with the
    timeout-governed wait_for_load_state before yielding.
    """
    for frame in page.frames:
        if frame is not page.main_frame and _skip_frame(frame.url):
            continue
        try:
            frame.wait_for_load_state("domcontentloaded", timeout=FRAME_OP_TIMEOUT_MS)
        except Exception:
            continue
        yield frame


def count_form_controls(page) -> dict:
    """Per-class control counts summed over all (plausible) frames."""
    total = {"textish": 0, "file": 0, "select": 0, "checkbox_radio": 0,
             "shadow": 0, "password": 0}
    with _frame_op_guard(page):
        for frame in _interesting_frames(page):
            try:
                counts = frame.evaluate(_COUNT_CONTROLS_JS)
                for key in total:
                    total[key] += counts[key]
            except Exception:
                continue
    total["light"] = (total["textish"] + total["file"]
                      + total["select"] + total["checkbox_radio"])
    return total


def looks_like_application_form(counts: dict) -> bool:
    """Text entry is the signature of an apply form; checkboxes alone are not.

    Shadow-only forms (>=2 controls hidden in shadow roots) count as found —
    the probe downgrades them to 'shadow-only' for special handling.
    """
    return (
        counts["textish"] + counts["file"] >= 2
        or (counts["textish"] >= 1 and counts["select"] >= 1)
        or counts["shadow"] >= 2
    )


def _acceptable_form(counts: dict) -> bool:
    """A password field means login/registration, not an application
    (lokale-kleinanzeigen lesson: its login form passed the size bar and
    the probe stopped one hop short of the real join.com form)."""
    return looks_like_application_form(counts) and not counts.get("password")


# Hosts whose presence in an anchor marks the real application destination.
# Tests may monkeypatch this to point at the fixture server.
_ATS_HOSTS = (
    "join.com", "greenhouse.io", "lever.co", "personio.de", "personio.com",
    "workable.com", "ashbyhq.com", "successfactors", "smartrecruiters.com",
    "recruitee.com", "softgarden", "myworkdayjobs.com", "teamtailor.com",
    "onlyfy", "heyrecruit",
)

_ATS_HREF_JS = """
(ats) => {
  const bad = /\\/(terms|privacy|legal|imprint|impressum|datenschutz|agb|login|signin|register)\\b/i;
  const cand = [...document.querySelectorAll('a[href]')].filter(a => {
    let u; try { u = new URL(a.href); } catch { return false; }
    if (!/^https?:$/.test(u.protocol) || bad.test(u.pathname)) return false;
    if (u.pathname === '/' || u.pathname === '') return false;
    return ats.some(d => u.hostname === d || u.hostname.endsWith('.' + d)
                         || u.hostname.includes(d));
  });
  const pref = cand.find(a => /bewerb|apply/i.test((a.innerText || '') + a.href));
  return (pref || cand[0]) ? (pref || cand[0]).href : null;
}
"""


# Wording that means the posting itself is gone (not just form-less).
_GONE_TEXT_RE = re.compile(
    r"nicht\s+mehr\s+(?:verfügbar|aktiv|online|vakant)|bereits\s+besetzt|"
    r"stelle\s+(?:ist|wurde)\s+(?:besetzt|deaktiviert|geschlossen)|"
    r"(?:anzeige|stellenanzeige)\s+(?:ist\s+)?abgelaufen|"
    r"no\s+longer\s+(?:available|active|accepting)|"
    r"position\s+has\s+been\s+filled|job\s+(?:has\s+)?expired|"
    r"vacancy\s+(?:is\s+)?closed|this\s+job\s+is\s+no\s+longer|"
    r"page\s+not\s+found|seite\s+nicht\s+gefunden", re.I)

_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}([_-][a-z]{2})?$", re.I)


def _gone_signal(page, requested_url: str, check_text: bool = True) -> str | None:
    """Posting-disappeared heuristics. The homepage-redirect check is pure
    URL logic and always meaningful — homepages carry search boxes that fool
    the raw control counts (Zenjob/heyjobs lesson). Gone wording is only
    consulted when the caller saw no acceptable form."""
    try:
        req, fin = urlparse(requested_url), urlparse(page.url)
        deep = [s for s in req.path.split("/") if s]
        shallow = [s for s in fin.path.split("/") if s]
        if (req.netloc == fin.netloc and len(deep) >= 2
                and (not shallow or (len(shallow) == 1
                                     and _LOCALE_SEGMENT_RE.match(shallow[0])))):
            return "redirected-to-homepage"
        if not check_text:
            return None
        text = page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 8000) : ''")
        m = _GONE_TEXT_RE.search(text or "")
        if m:
            return f"gone-text: {m.group(0)[:60]}"
    except Exception:
        pass
    return None


def _find_ats_href(page) -> str | None:
    """An anchor on the page that points into a known ATS, if any."""
    try:
        return page.evaluate(_ATS_HREF_JS, list(_ATS_HOSTS))
    except Exception:
        return None


# Pick the anchor whose visible text matches the job title — and ONLY when it
# is an unambiguous winner. A careers list page (Workato) drops you on a roster
# of postings; the real form is one hop behind the matching title link.
# Blind apply-button clicking here would risk submitting to the wrong job, so
# we demand a clear title match instead (watchlist #10).
_TITLE_HREF_JS = """
(title) => {
  const norm = s => (s || '').toLowerCase()
      .replace(/[^a-z0-9äöüß ]+/g, ' ').replace(/\\s+/g, ' ').trim();
  const tTok = new Set(norm(title).split(' ').filter(w => w.length > 2));
  if (tTok.size === 0) return null;
  const scored = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let u; try { u = new URL(a.href); } catch { continue; }
    if (!/^https?:$/.test(u.protocol)) continue;
    const aTok = new Set(norm(a.innerText || a.textContent).split(' ').filter(w => w.length > 2));
    if (aTok.size === 0) continue;
    let inter = 0; for (const w of tTok) if (aTok.has(w)) inter++;
    scored.push({ href: a.href, score: inter / tTok.size });
  }
  scored.sort((x, y) => y.score - x.score);
  if (scored.length === 0 || scored[0].score < 0.8) return null;
  if (scored.length > 1 && scored[1].score >= scored[0].score) return null;  // ambiguous
  return scored[0].href;
}
"""


def _find_title_href(page, title: str) -> str | None:
    """Anchor whose text unambiguously matches the job title, if any."""
    if not title:
        return None
    try:
        return page.evaluate(_TITLE_HREF_JS, title)
    except Exception:
        return None


def detect_captcha(page) -> bool:
    for frame in page.frames:
        try:
            if any(m in frame.url.lower() for m in _CAPTCHA_MARKERS):
                return True
            if frame is page.main_frame and any(
                m in (frame.content() or "").lower() for m in _CAPTCHA_MARKERS
            ):
                return True
        except Exception:
            continue
    return False


def _find_apply_control(page):
    for pattern in APPLY_BUTTON_PATTERNS:
        rx = re.compile(pattern, re.IGNORECASE)
        for finder in (
            lambda: page.get_by_role("button", name=rx),
            lambda: page.get_by_role("link", name=rx),
            lambda: page.locator("button, a, [role=button]").filter(has_text=rx),
        ):
            try:
                loc = finder().first
                if loc.is_visible(timeout=CLICK_TIMEOUT_MS):
                    return loc, pattern
            except Exception:
                continue
    return None, None


def goto_apply_page(page, url: str, title: str | None = None) -> dict:
    """Navigate to a job URL and end up on its application form if possible.

    Strategy: load → settle → answer cookie wall (necessary only) → if what's
    visible doesn't look like an apply form, follow one apply-looking
    button/link (new-tab aware) → settle → cookie again. When a job `title`
    is given and the page is still form-less, follow an unambiguous
    title-matched link one hop (careers list → posting).

    Returns a report dict; report['page'] is the page that ends up holding
    the form (a popup if the apply button opened one) — callers that
    serialise the report must pop it first. Never raises on 'form not found'.
    """
    report = {
        "url": url, "final_url": None, "cookie_clicked": None,
        "clicked_apply": None, "opened_new_tab": False, "form_found": False,
        "captcha": False, "controls": {}, "error": None, "page": page,
        "gone_signal": None,
    }
    active = page
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        _settle(page)
        report["cookie_clicked"] = dismiss_cookie_banner(page)
        controls = count_form_controls(page)

        if not _acceptable_form(controls):
            loc, pattern = _find_apply_control(page)
            if loc is not None:
                report["clicked_apply"] = pattern
                href = None
                try:
                    href = loc.get_attribute("href", timeout=1_000)
                except Exception:
                    pass
                if href and not href.lower().startswith(("javascript:", "#", "mailto:")):
                    # a real link: navigating its href beats simulating a click
                    # (immune to overlay interception and target=_blank races)
                    page.goto(urljoin(page.url, href),
                              wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                else:
                    try:
                        # apply buttons on board/career pages often target=_blank;
                        # generous timeouts — SPA buttons aren't actionable until
                        # hydration finishes (WAD needs ~5-8s on a cold profile)
                        with page.context.expect_page(timeout=12_000) as popup:
                            loc.click(timeout=8_000)
                        active = popup.value
                        report["opened_new_tab"] = True
                    except Exception:
                        pass  # no popup — same-tab navigation (or a no-op click)
                try:
                    active.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    _settle(active)
                    cookie2 = dismiss_cookie_banner(active)
                    report["cookie_clicked"] = report["cookie_clicked"] or cookie2
                    controls = count_form_controls(active)
                except Exception:
                    pass
                if active is page and not _acceptable_form(controls):
                    # click led nowhere (blocked popup / no-op) — try a DOM href
                    try:
                        href2 = page.evaluate(_APPLY_HREF_JS)
                        if href2:
                            page.goto(href2, wait_until="domcontentloaded",
                                      timeout=NAV_TIMEOUT_MS)
                            _settle(page)
                            report["cookie_clicked"] = (report["cookie_clicked"]
                                                        or dismiss_cookie_banner(page))
                            report["clicked_apply"] = f"{pattern} (href fallback)"
                            controls = count_form_controls(page)
                    except Exception:
                        pass

        if not _acceptable_form(controls):
            # last resort: boards/classifieds reposts often bury the real
            # application one more hop away behind a known-ATS link
            ats_href = _find_ats_href(active)
            if (ats_href and urlparse(ats_href).netloc
                    != urlparse(active.url).netloc):
                try:
                    active.goto(ats_href, wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
                    _settle(active)
                    report["cookie_clicked"] = (report["cookie_clicked"]
                                                or dismiss_cookie_banner(active))
                    report["clicked_apply"] = ((report.get("clicked_apply") or "")
                                               + " +ats-pull-through").strip()
                    controls = count_form_controls(active)
                except Exception:
                    pass

        if title and not _acceptable_form(controls):
            # careers list page (Workato): the form is behind the link whose
            # text matches THIS job's title — follow it only if unambiguous
            title_href = _find_title_href(active, title)
            if title_href and title_href != active.url:
                try:
                    active.goto(title_href, wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
                    _settle(active)
                    report["cookie_clicked"] = (report["cookie_clicked"]
                                                or dismiss_cookie_banner(active))
                    report["clicked_apply"] = ((report.get("clicked_apply") or "")
                                               + " +title-hop").strip()
                    controls = count_form_controls(active)
                except Exception:
                    pass

        report["page"] = active
        report["final_url"] = active.url
        report["controls"] = controls
        report["form_found"] = _acceptable_form(controls)
        report["gone_signal"] = _gone_signal(
            active, url, check_text=not report["form_found"])
        report["captcha"] = detect_captcha(active)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:300]
        try:
            report["final_url"] = active.url
        except Exception:
            pass
    return report


def _frame_selector(frame) -> str | None:
    """Stable-ish selector for the iframe element hosting a child frame."""
    try:
        el = frame.frame_element()
        info = el.evaluate(
            "e => ({id: e.id || '', name: e.name || '', src: e.getAttribute('src') || ''})"
        )
    except Exception:
        return None
    if info["id"]:
        return f"iframe#{info['id']}"
    if info["name"]:
        return f'iframe[name="{info["name"]}"]'
    if info["src"]:
        return f'iframe[src="{info["src"]}"]'
    return "iframe"


def extract_form_tree(page) -> dict:
    """Field node tables + pruned HTML for every frame that holds controls.

    Returns {'fields': [FormField...], 'pruned': {frame_key: html},
             'frames': [frame_key...], 'shadow_controls': int}.
    Shadow-DOM-only controls are counted but not extractable from
    frame.content() — Step 4 sees the count and can route those to vision
    or manual handling.
    """
    fields: list[FormField] = []
    pruned: dict[str, str] = {}
    frames_used: list[str] = []
    seen: set[tuple] = set()  # the same widget often renders in main doc AND
                              # an iframe — one (selector, kind, label) is enough

    def frame_path_of(frame) -> tuple[str, ...] | None:
        path: list[str] = []
        node = frame
        while node is not page.main_frame:
            sel = _frame_selector(node)
            if sel is None:
                return None
            path.append(sel)
            node = node.parent_frame
        return tuple(reversed(path))

    for frame in _interesting_frames(page):
        try:
            html = frame.content()
        except Exception:
            continue
        path = frame_path_of(frame)
        if path is None:
            continue
        frame_fields = [
            f for f in extract_fields(html, frame_path=path)
            if (f.selector, f.kind, f.label) not in seen
        ]
        if not frame_fields:
            continue
        seen.update((f.selector, f.kind, f.label) for f in frame_fields)
        key = " > ".join(path) or "(main)"
        fields.extend(frame_fields)
        pruned[key] = prune_html(html)
        frames_used.append(key)

    return {
        "fields": fields,
        "pruned": pruned,
        "frames": frames_used,
        "shadow_controls": count_form_controls(page)["shadow"],
    }
