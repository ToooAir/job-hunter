"""Shared test guard: is a *launchable* Chromium available?

The host venv installs the playwright package for CDP attach mode but never
downloads browser binaries — `import playwright` succeeding is not enough
for tests that launch headless sessions. Checked once per test run.
"""

from pathlib import Path

_cached: bool | None = None


def has_chromium() -> bool:
    global _cached
    if _cached is None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                _cached = Path(pw.chromium.executable_path).exists()
        except Exception:
            _cached = False
    return _cached
