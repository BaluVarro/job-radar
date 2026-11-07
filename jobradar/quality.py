"""Data-quality checks: validate each scraped posting and flag unusual runs.

- validate():  rejects postings with missing/odd fields before they reach the database
- anomaly():   compares today's count for a source with its previous successful run, so a
               broken scraper (site redesign, blocked request) is noticed instead of silently
               reporting "no jobs"
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .models import Job

MIN_TITLE, MAX_TITLE = 4, 200


def validate(job: Job) -> list[str]:
    problems = []
    if not job.title:
        problems.append("missing title")
    elif not (MIN_TITLE <= len(job.title) <= MAX_TITLE):
        problems.append(f"title length {len(job.title)} outside {MIN_TITLE}-{MAX_TITLE}")
    parts = urlsplit(job.url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        problems.append("invalid url")
    if " " in job.url:
        problems.append("url contains spaces")
    return problems


def anomaly(previous: int | None, current: int) -> str | None:
    """Return a warning code if today's count looks wrong compared with last time."""
    if previous is None:
        return "first_run" if current == 0 else None
    if previous >= 3 and current == 0:
        return "dropped_to_zero"
    if previous >= 10 and current < 0.3 * previous:
        return "sharp_drop"
    if current > 5 * max(previous, 4):
        return "spike"           # often means navigation links were picked up as jobs
    return None


def duplicate_rate(jobs: list[Job]) -> float:
    if not jobs:
        return 0.0
    return 1 - len({j.uid for j in jobs}) / len(jobs)
