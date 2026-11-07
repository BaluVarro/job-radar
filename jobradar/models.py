"""Data model for a single job posting."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track where a click came from; they don't identify a job.
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "gclid", "fbclid", "src", "source", "ref", "codes"}


def normalize_url(url: str) -> str:
    """Canonical form of a URL so the same job isn't stored twice."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


@dataclass
class Job:
    company_id: str
    company: str
    category: str
    title: str
    url: str
    location: str = ""
    posted: str = ""      # ISO date if the source provides one
    source: str = ""      # adapter that produced it

    def __post_init__(self) -> None:
        self.title = " ".join(self.title.split())
        self.location = " ".join(self.location.split())
        self.url = self.url.strip()

    @property
    def uid(self) -> str:
        key = f"{self.company_id}|{normalize_url(self.url)}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        return d
