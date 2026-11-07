"""Base class shared by all adapters."""

from __future__ import annotations

from typing import Any

import requests

from ..models import Job


class Adapter:
    name = "base"

    def __init__(self, company: dict[str, Any], session: requests.Session, **context: Any):
        self.company = company
        self.session = session
        self.options: dict[str, Any] = company.get("options") or {}
        self.context = context

    def make_job(self, title: str, url: str, location: str = "", posted: str = "") -> Job:
        return Job(
            company_id=self.company["id"],
            company=self.company["name"],
            category=self.company.get("category", ""),
            title=title,
            url=url,
            location=location or self.company.get("default_location", ""),
            posted=posted,
            source=self.name,
        )

    def fetch(self) -> list[Job]:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError
