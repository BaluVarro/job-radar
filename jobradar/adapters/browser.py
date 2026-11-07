"""For careers pages that only show jobs after JavaScript runs.

Renders the page in headless Chromium (Playwright), then uses the same link
extraction as the html adapter. Extra options:

  wait_for: CSS selector to wait for before reading the page (e.g. "a[href*='/job/']")
  scrolls:  how many times to scroll down to trigger lazy loading (default 2)
"""

from __future__ import annotations

from ..models import Job
from .html import HtmlAdapter


class BrowserManager:
    """One shared Chromium instance for the whole run (started only if needed)."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    def page(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        context = self._browser.new_context(locale="en-US")
        return context.new_page()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
            self._browser = None


class BrowserAdapter(HtmlAdapter):
    name = "browser"

    def fetch(self) -> list[Job]:
        manager: BrowserManager | None = self.context.get("browser")
        if manager is None:
            raise RuntimeError("browser adapter disabled for this run (--no-browser)")
        page = manager.page()
        try:
            page.goto(self.company["url"], wait_until="domcontentloaded", timeout=45_000)
            if self.options.get("wait_for"):
                page.wait_for_selector(self.options["wait_for"], timeout=20_000)
            else:
                page.wait_for_load_state("networkidle", timeout=20_000)
            for _ in range(int(self.options.get("scrolls", 2))):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1000)
            html, final_url = page.content(), page.url
        finally:
            page.context.close()
        return self.parse(html, base_url=final_url)
