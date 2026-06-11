"""browser.py — shared browser layer for the semi-auto apply agent (Step 3).

Two backends behind the same Playwright Page/Context interface:
  * headless_session(): persistent-profile headless Chromium for Stage 1
    (unattended schema extraction). Runs fine inside the pipeline container;
    the profile lives on the mounted data volume so cookie consent sticks.
  * cdp_session(): attach to the user's real Chrome via CDP for Stage 2
    (user-present submission). Host-only — from inside the container the
    advertised ws://127.0.0.1 endpoint resolves to the container itself.

Policy: cookie walls are answered with "necessary only", never "accept all".
Captchas are detected and reported, never bypassed.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from utils.dom_pruner import FormField, extract_fields, prune_html

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = Path(os.getenv("BROWSER_PROFILE_DIR", str(ROOT / "data" / "browser_profile")))
CDP_ENDPOINT = os.getenv("CDP_ENDPOINT", "http://127.0.0.1:9222")

NAV_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 8_000
CLICK_TIMEOUT_MS = 1_200

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

_COUNT_CONTROLS_JS = """
() => {
  const SEL = 'input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select';
  let light = document.querySelectorAll(SEL).length;
  let shadow = 0;
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        shadow += el.shadowRoot.querySelectorAll(SEL).length;
        walk(el.shadowRoot);
      }
    }
  };
  walk(document);
  return {light, shadow};
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


@contextmanager
def cdp_session(endpoint: str | None = None):
    """Attach to the user's real Chrome (dedicated profile, host-only).

    Closing the session disconnects; it never kills the user's browser or
    their existing tabs/contexts.
    """
    endpoint = endpoint or CDP_ENDPOINT
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            yield context
        finally:
            browser.close()  # disconnect only — browser was not launched by us


def dismiss_cookie_banner(page) -> str | None:
    """Click the 'necessary only' consent option. Returns clicked text or None.

    get_by_role pierces open shadow DOM (Usercentrics et al.); consent iframes
    (Cookiebot) are searched too. Best effort: a missed banner shows up later
    as form_found=False in the probe report, not as a crash.
    """
    scopes = [page] + [f for f in page.frames if f is not page.main_frame]
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


def count_form_controls(page) -> dict:
    """{'light': n, 'shadow': n} summed over all frames."""
    total = {"light": 0, "shadow": 0}
    for frame in page.frames:
        try:
            counts = frame.evaluate(_COUNT_CONTROLS_JS)
            total["light"] += counts["light"]
            total["shadow"] += counts["shadow"]
        except Exception:
            continue
    return total


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


def goto_apply_page(page, url: str, min_controls: int = 2) -> dict:
    """Navigate to a job URL and end up on its application form if possible.

    Strategy: load → settle → answer cookie wall (necessary only) → if no form
    is visible, follow one apply-looking button/link → settle → cookie again.
    Returns a report dict; never raises on 'form not found'.
    """
    report = {
        "url": url, "final_url": None, "cookie_clicked": None,
        "clicked_apply": None, "form_found": False, "captcha": False,
        "controls": {"light": 0, "shadow": 0}, "error": None,
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        _settle(page)
        report["cookie_clicked"] = dismiss_cookie_banner(page)
        controls = count_form_controls(page)

        if controls["light"] + controls["shadow"] < min_controls:
            loc, pattern = _find_apply_control(page)
            if loc is not None:
                try:
                    loc.click(timeout=CLICK_TIMEOUT_MS * 3)
                    report["clicked_apply"] = pattern
                    page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    _settle(page)
                    cookie2 = dismiss_cookie_banner(page)
                    report["cookie_clicked"] = report["cookie_clicked"] or cookie2
                    controls = count_form_controls(page)
                except Exception:
                    pass

        report["final_url"] = page.url
        report["controls"] = controls
        report["form_found"] = controls["light"] + controls["shadow"] >= min_controls
        report["captcha"] = detect_captcha(page)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:300]
        try:
            report["final_url"] = page.url
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

    for frame in page.frames:
        try:
            html = frame.content()
        except Exception:
            continue
        path = frame_path_of(frame)
        if path is None:
            continue
        frame_fields = extract_fields(html, frame_path=path)
        if not frame_fields:
            continue
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
