"""Generic adapter for server-rendered careers pages.

It reads every link on the page and keeps the ones that look like job postings.
Per-company options (all optional) let you tune it in config/companies.yaml:

  link_pattern:    regex the link URL must match (e.g. "/job/")
  exclude_pattern: regex for links to drop (e.g. "login|search")
  same_domain:     only keep links on the same site (default false)
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import http
from ..models import Job
from .base import Adapter

# Used when a company has no link_pattern: URL fragments that usually mean "a job".
DEFAULT_LINK_PATTERN = re.compile(
    r"/jobs?/|/job-|jobid|job_id|/allas|/%C3%A1ll%C3%A1s|/position|/vacanc|/opening|"
    r"/requisition|/JobDetail|/careers?/.*\d{4,}|/palyazat|/p%C3%A1ly%C3%A1zat",
    re.IGNORECASE,
)

# Link texts that are navigation, not job titles.
NAV_TEXT = re.compile(
    r"^(all jobs|view all|see all|search|apply( now)?|log ?in|sign ?in|register|next|previous|"
    r"more|read more|details|tovább|bővebben|jelentkez(és|em)|összes állás|állások|karrier|"
    r"careers?|jobs?|home|főoldal|back|vissza|\d+)$",
    re.IGNORECASE,
)

TITLE_MIN, TITLE_MAX = 4, 200


class HtmlAdapter(Adapter):
    name = "html"

    def fetch(self) -> list[Job]:
        resp = http.get(self.session, self.company["url"])
        return self.parse(resp.text, base_url=resp.url)

    def parse(self, html: str, base_url: str | None = None) -> list[Job]:
        base_url = base_url or self.company["url"]
        soup = BeautifulSoup(html, "lxml")
        link_re = (re.compile(self.options["link_pattern"], re.IGNORECASE)
                   if self.options.get("link_pattern") else DEFAULT_LINK_PATTERN)
        exclude_re = (re.compile(self.options["exclude_pattern"], re.IGNORECASE)
                      if self.options.get("exclude_pattern") else None)
        base_host = urlsplit(base_url).netloc

        jobs: list[Job] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            url = urljoin(base_url, href)
            if url in seen or not link_re.search(url):
                continue
            if exclude_re and exclude_re.search(url):
                continue
            if self.options.get("same_domain") and urlsplit(url).netloc != base_host:
                continue
            title = self._title_for(a)
            if not (TITLE_MIN <= len(title) <= TITLE_MAX) or NAV_TEXT.match(title):
                continue
            seen.add(url)
            jobs.append(self.make_job(title=title, url=url, location=self._location_for(a)))
        return jobs

    @staticmethod
    def _title_for(a) -> str:
        text = a.get_text(" ", strip=True)
        if not text:
            text = a.get("title", "") or a.get("aria-label", "")
        return " ".join(text.split())

    @staticmethod
    def _location_for(a) -> str:
        """Many job lists put the location next to the link in the same list item/row."""
        container = a.find_parent(["li", "tr", "article"])
        if not container:
            return ""
        el = container.find(class_=re.compile(r"location|city|helyszin|place", re.IGNORECASE))
        return el.get_text(" ", strip=True) if el else ""
