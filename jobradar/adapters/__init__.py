"""Adapters turn one company's careers page into a list of Job objects.

Each adapter handles one kind of source:
  greenhouse - Greenhouse public job board API (JSON)
  workday    - Workday career sites (JSON endpoint behind the page)
  hrxml      - HR-XML job feeds (e.g. Erste Bank)
  html       - server-rendered careers pages (generic link extraction)
  browser    - JavaScript-heavy pages, rendered in headless Chromium first
"""

from .base import Adapter
from .browser import BrowserAdapter
from .greenhouse import GreenhouseAdapter
from .hrxml import HrXmlAdapter
from .html import HtmlAdapter
from .workday import WorkdayAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "greenhouse": GreenhouseAdapter,
    "workday": WorkdayAdapter,
    "hrxml": HrXmlAdapter,
    "html": HtmlAdapter,
    "browser": BrowserAdapter,
}

__all__ = ["ADAPTERS", "Adapter"]
