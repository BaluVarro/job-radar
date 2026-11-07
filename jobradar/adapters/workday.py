"""Workday career sites expose a JSON search endpoint behind the page.

Config options:
  host:   e.g. pwc.wd3.myworkdayjobs.com
  tenant: e.g. pwc
  site:   e.g. Global_Campus_Careers
  search_text: optional free-text search, e.g. "Hungary"
"""

from __future__ import annotations

from .. import http
from ..models import Job
from .base import Adapter

PAGE_SIZE = 20
MAX_JOBS = 200


class WorkdayAdapter(Adapter):
    name = "workday"

    def fetch(self) -> list[Job]:
        host, tenant, site = self.options["host"], self.options["tenant"], self.options["site"]
        endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        jobs: list[Job] = []
        offset = 0
        while offset < MAX_JOBS:
            payload = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset,
                       "searchText": self.options.get("search_text", "")}
            data = http.post(self.session, endpoint, json=payload,
                             headers={"Accept": "application/json"}).json()
            page = self.parse(data)
            jobs.extend(page)
            offset += PAGE_SIZE
            if not page or offset >= int(data.get("total", 0)):
                break
        return jobs

    def parse(self, data: dict) -> list[Job]:
        host, site = self.options["host"], self.options["site"]
        jobs = []
        for item in data.get("jobPostings", []):
            path = item.get("externalPath", "")
            jobs.append(self.make_job(
                title=item.get("title", ""),
                url=f"https://{host}/en-US/{site}{path}",
                location=item.get("locationsText", ""),
                posted=item.get("postedOn", ""),   # e.g. "Posted 3 Days Ago"
            ))
        return jobs
