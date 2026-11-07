"""Decides which postings are relevant, using config/filters.yaml.

Every posting is stored; this only sets the `match` flag the viewer uses by default,
so you can always switch to "show all" and tune the filters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Job


@dataclass
class Filters:
    include_roles: list[re.Pattern]
    exclude_roles: list[re.Pattern]
    locations: list[re.Pattern]

    @classmethod
    def from_config(cls, cfg: dict) -> "Filters":
        def compile_all(items):
            return [re.compile(p, re.IGNORECASE) for p in items or []]
        return cls(compile_all(cfg.get("include_roles")),
                   compile_all(cfg.get("exclude_roles")),
                   compile_all(cfg.get("locations")))

    def role_ok(self, job: Job) -> bool:
        title = job.title
        if any(p.search(title) for p in self.exclude_roles):
            return False
        return any(p.search(title) for p in self.include_roles)

    def location_ok(self, job: Job) -> bool:
        # Location text first; fall back to the URL and title, which often contain the city.
        haystack = f"{job.location} {job.url} {job.title}"
        return any(p.search(haystack) for p in self.locations)

    def matches(self, job: Job) -> bool:
        return self.role_ok(job) and self.location_ok(job)
