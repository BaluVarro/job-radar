"""Greenhouse public job board API: https://developers.greenhouse.io/job-board.html"""

from __future__ import annotations

from .. import http
from ..models import Job
from .base import Adapter


class GreenhouseAdapter(Adapter):
    name = "greenhouse"

    def fetch(self) -> list[Job]:
        token = self.options["board_token"]
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        data = http.get(self.session, url).json()
        return self.parse(data)

    def parse(self, data: dict) -> list[Job]:
        jobs = []
        for item in data.get("jobs", []):
            jobs.append(self.make_job(
                title=item.get("title", ""),
                url=item.get("absolute_url", ""),
                location=(item.get("location") or {}).get("name", ""),
                posted=(item.get("updated_at") or "")[:10],
            ))
        return jobs
