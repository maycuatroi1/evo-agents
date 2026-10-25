"""Playwright behind the ``Page`` of ``evo_agents.hub.browser_post``: the one module that imports it, and only when a
page is opened, so a machine without Playwright installs and runs everything else. The tests of the script use a fake page;
nothing here is run by them (a real Chromium and a real platform are the member's to try, ``docs/marketing-plugin.md`` of evo-agents-core-plugins).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

NAV_TIMEOUT_MS = 30_000
IDLE_TIMEOUT_MS = 15_000


class BrowserUnavailable(Exception):
    """Playwright or its Chromium is not on this machine."""


class PlaywrightPage:
    def __init__(self, page):
        self._page = page
        self.status = 0
        page.on("response", self._seen)

    def _seen(self, response) -> None:
        if response.request.is_navigation_request() and response.frame == self._page.main_frame:
            self.status = response.status

    @property
    def url(self) -> str:
        return self._page.url

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    def text(self) -> str:
        return self._page.inner_text("body")

    def has(self, selector: str) -> bool:
        return self._page.locator(selector).count() > 0

    def fill(self, selector: str, value: str) -> None:
        self._page.fill(selector, value, timeout=NAV_TIMEOUT_MS)

    def click(self, selector: str) -> None:
        self._page.click(selector, timeout=NAV_TIMEOUT_MS)

    def settle(self) -> None:
        try:
            self._page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
        except Exception:  # a page that never goes idle is read as it is
            pass

    def screenshot(self) -> bytes:
        return self._page.screenshot(type="png")


@contextmanager
def open_page(session: str | None, *, headless: bool = True) -> Iterator[tuple[PlaywrightPage, object]]:
    """A page of a fresh Chromium that holds the login ``session`` (Playwright's storage state as JSON, None for none)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright is not installed here: `pip install playwright && playwright install chromium`"
        ) from exc
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=json.loads(session) if session else None)
        try:
            yield PlaywrightPage(context.new_page()), context
        finally:
            context.close()
            browser.close()


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True
