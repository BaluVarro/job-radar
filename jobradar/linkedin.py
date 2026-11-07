"""LinkedIn job search driven by your profile – run on your own computer only.

    python -m jobradar.linkedin            # search, score, open linkedin_results.html
    python -m jobradar.linkedin --dry-run  # only print the searches it would make

How it works
  1. Builds searches from the `linkedin:` section of config/profile.yaml
     (keywords from your CV x locations, filtered to internship/entry level, recent postings).
  2. Calls LinkedIn's public, logged-out job search (the same results anyone sees without
     an account). No login, no cookies, nothing tied to your LinkedIn account.
  3. Reads the public page of the most promising postings and scores everything with the
     same scorer as the careers-page radar.
  4. Saves results locally (data/linkedin.db, linkedin_results.html). Both are in .gitignore,
     so LinkedIn data never goes to GitHub or your public page.

Ground rules (automated access is against LinkedIn's terms even when logged out, so this is
for occasional personal use): a hard cap on requests per run, several seconds between
requests, and if LinkedIn refuses or rate-limits a request, the run stops immediately and
keeps what it has. No retries, proxies or disguises.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import yaml
from bs4 import BeautifulSoup

from .filters import Filters
from .http import make_session, TIMEOUT
from .models import Job
from .scorer import Scorer
from .storage import Store

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("jobradar.linkedin")

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
EXPERIENCE_CODES = {"internship": "1", "entry": "2", "associate": "3"}
STOP_STATUSES = {401, 403, 429, 999}      # 999 = LinkedIn's "request denied" code
MAX_REQUESTS = 40                         # hard cap per run, whatever the settings say
PAGE_SIZE = 25                            # LinkedIn returns 25 cards per page
MIN_DELAY = 3.0                           # seconds between requests, at least

COMPANY_ID = "linkedin"


class LinkedInStopped(Exception):
    """LinkedIn refused or rate-limited a request: stop the run."""


class RequestCapReached(Exception):
    """This run used its request budget: finish with what we have."""


# --- building searches ---------------------------------------------------------------

def build_searches(cfg: dict) -> list[dict]:
    """One search per keyword x location, with the profile's filters."""
    experience = ",".join(EXPERIENCE_CODES[e] for e in cfg.get("experience", ["internship", "entry"]))
    days = int(cfg.get("posted_within_days", 7))
    searches = []
    for keyword in cfg.get("keywords", []):
        for loc in cfg.get("locations", []):
            params = {"keywords": keyword, "location": loc["location"], "f_E": experience,
                      "f_TPR": f"r{days * 86400}"}
            if loc.get("remote"):
                params["f_WT"] = "2"            # remote only
            for page in range(int(cfg.get("pages_per_query", 1))):
                searches.append({"label": f"{keyword} · {loc['name']}",
                                 "params": {**params, "start": page * PAGE_SIZE}})
    return searches


# --- parsing ---------------------------------------------------------------------------

def _text(node, selector: str) -> str:
    el = node.select_one(selector)
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def parse_search(html: str) -> list[dict]:
    """Job cards from a search results page."""
    soup = BeautifulSoup(html, "lxml")
    cards = []
    for card in soup.select("div.base-card, div.base-search-card, li > div[data-entity-urn]"):
        urn = card.get("data-entity-urn", "")
        link = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        href = link["href"] if link and link.has_attr("href") else ""
        job_id = (re.search(r"jobPosting:(\d+)", urn) or re.search(r"-(\d{6,})(?:\?|$)", href)
                  or re.search(r"/view/(\d{6,})", href))
        title = _text(card, ".base-search-card__title")
        if not job_id or not title:
            continue
        time_el = card.select_one("time")
        cards.append({
            "job_id": job_id.group(1),
            "title": title,
            "company": _text(card, ".base-search-card__subtitle"),
            "location": _text(card, ".job-search-card__location"),
            "posted": time_el.get("datetime", "") if time_el else "",
            "url": f"https://www.linkedin.com/jobs/view/{job_id.group(1)}",
        })
    return cards


def parse_detail(html: str) -> str:
    """Description text plus LinkedIn's criteria (seniority, employment type...)."""
    soup = BeautifulSoup(html, "lxml")
    desc = soup.select_one(".show-more-less-html__markup, .description__text")
    text = desc.get_text("\n", strip=True) if desc else ""
    criteria = []
    for item in soup.select(".description__job-criteria-item"):
        name = _text(item, ".description__job-criteria-subheader")
        value = _text(item, ".description__job-criteria-text")
        if name and value:
            criteria.append(f"{name}: {value}")
    return "\n".join(criteria + [text]).strip()


# --- fetching --------------------------------------------------------------------------

class PoliteClient:
    """Counts requests, waits between them, and stops on the first refusal."""

    def __init__(self, session, delay: float, max_requests: int = MAX_REQUESTS):
        self.session = session
        self.delay = max(delay, MIN_DELAY)    # never faster than one request per 3 s
        self.max_requests = min(max_requests, MAX_REQUESTS)
        self.count = 0
        self._last = 0.0

    def get(self, url: str, params: dict | None = None) -> str:
        if self.count >= self.max_requests:
            raise RequestCapReached(f"request cap reached ({self.max_requests})")
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.count += 1
        resp = self.session.get(url, params=params, timeout=TIMEOUT)
        if resp.status_code in STOP_STATUSES:
            raise LinkedInStopped(f"LinkedIn answered HTTP {resp.status_code} – stopping")
        resp.raise_for_status()
        return resp.text


# --- the run ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9áéíóöőúüű]+", " ", s.lower()).strip()


def careers_page_keys(root: Path) -> set[tuple[str, str]]:
    """(company, title) of postings the careers-page radar already has, to flag duplicates."""
    path = root / "data" / "jobs.db"
    if not path.exists():
        return set()
    store = Store(path)
    keys = {(_norm(j["company"]), _norm(j["title"])) for j in store.active_jobs()}
    store.db.close()
    return keys


def run(root: Path = ROOT, session=None, dry_run: bool = False, open_browser: bool = True) -> dict:
    profile = yaml.safe_load((root / "config" / "profile.yaml").read_text(encoding="utf-8"))
    cfg = profile.get("linkedin") or {}
    searches = build_searches(cfg)
    if dry_run:
        for s in searches:
            print(f"{s['label']:45}  {SEARCH_URL}?{urlencode(s['params'])}")
        print(f"\n{len(searches)} searches (+ up to {cfg.get('describe_top', 20)} job pages), "
              f"cap {MAX_REQUESTS} requests")
        return {"searches": len(searches)}

    filters = Filters.from_config(
        yaml.safe_load((root / "config" / "filters.yaml").read_text(encoding="utf-8")))
    scorer = Scorer(profile)
    client = PoliteClient(session or make_session(), float(cfg.get("delay_seconds", 5)),
                          int(cfg.get("max_requests", MAX_REQUESTS)))
    started = datetime.now(timezone.utc)
    today = started.date().isoformat()
    stopped = None          # set if LinkedIn refused a request
    capped = False          # set if this run used its request budget

    # 1. Search.
    cards: dict[str, dict] = {}
    for s in searches:
        try:
            found = parse_search(client.get(SEARCH_URL, s["params"]))
        except LinkedInStopped as exc:
            stopped = str(exc)
            break
        except RequestCapReached:
            capped = True
            break
        except Exception as exc:
            log.warning("search '%s' failed: %s", s["label"], exc)
            continue
        log.info("%-45s %d cards", s["label"], len(found))
        for c in found:
            c.setdefault("queries", [])
            cards.setdefault(c["job_id"], c)["queries"].append(s["label"])

    jobs = {c["job_id"]: Job(COMPANY_ID, c["company"] or "Unknown company", "LinkedIn",
                             c["title"], c["url"], c["location"], c["posted"], source="linkedin")
            for c in cards.values()}

    # 2. Read the most promising postings (highest title-only score first).
    store = Store(root / "data" / "linkedin.db")
    ranked = sorted(jobs.values(), key=lambda j: -scorer.score(j.title, "", j.location, j.url).total)
    describe_top = int(cfg.get("describe_top", 20))
    for job in [j for j in ranked if store.needs_description(j.uid)][:describe_top]:
        if stopped or capped:
            break
        job_id = urlsplit(job.url).path.rstrip("/").split("/")[-1]
        try:
            text = parse_detail(client.get(DETAIL_URL.format(job_id=job_id)))
            store.save_description(job.uid, text, "ok" if len(text) >= 200 else "empty", today)
        except LinkedInStopped as exc:
            stopped = str(exc)
        except RequestCapReached:
            capped = True
        except Exception as exc:
            log.warning("job page %s failed: %s", job.url, exc)
            store.save_description(job.uid, "", "error", today)

    # 3. Store, score, flag duplicates of careers-page postings.
    job_list = list(jobs.values())
    matches = {j.uid: filters.matches(j) for j in job_list}
    new_uids = set(store.upsert_jobs(job_list, matches, today))
    if not stopped and not capped:      # only a complete run can tell what disappeared
        store.close_missing(COMPANY_ID, {j.uid for j in job_list})
    texts = store.descriptions([j.uid for j in job_list])
    on_site = careers_page_keys(root)
    for job in job_list:
        result = scorer.score(job.title, texts.get(job.uid, ""), job.location, job.url)
        reasons = list(result.reasons)
        if (_norm(job.company), _norm(job.title)) in on_site:
            reasons.append("+ also on the company's own careers page")
        store.set_score(job.uid, result.total, json.dumps(reasons, ensure_ascii=False))
    store.db.commit()

    results = [j for j in store.active_jobs() if j["uid"] in {x.uid for x in job_list}]
    for j in results:
        j["is_new"] = j["uid"] in new_uids
        j["score_reasons"] = json.loads(j["score_reasons"] or "[]")
        j["queries"] = cards.get(urlsplit(j["url"]).path.rstrip("/").split("/")[-1], {}).get("queries", [])
    store.close()

    summary = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "searches": len(searches), "requests": client.count, "postings": len(results),
               "new": len(new_uids), "stopped": stopped, "capped": capped}
    out = write_results_page(root, results, summary)

    print_top(results)
    if stopped:
        print(f"\nStopped early: {stopped}. Results so far are saved. "
              "Wait a day or two before running again.")
    elif capped:
        print(f"\nReached the {client.max_requests}-request limit; the remaining job pages "
              "will be read next run.")
    print(f"\n{len(results)} postings ({len(new_uids)} new) from {client.count} requests → {out}")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return summary


def print_top(results: list[dict], top: int = 15) -> None:
    for j in sorted(results, key=lambda j: -(j["score"] or 0))[:top]:
        new = " NEW" if j["is_new"] else ""
        print(f"{j['score']:3d}  {j['company'][:22]:22}  {j['title'][:60]}{new}")


def write_results_page(root: Path, results: list[dict], summary: dict) -> Path:
    """A single, self-contained HTML file (the radar viewer with the data built in),
    so it opens straight from your disk without a web server."""
    template = (root / "docs" / "index.html").read_text(encoding="utf-8")
    data = {"generated": summary["generated"], "source": "LinkedIn", "summary": summary,
            "jobs": results}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = template.replace("<title>Job Radar</title>", "<title>Job Radar – LinkedIn</title>")
    page = page.replace("<h1>Job Radar</h1>", "<h1>Job Radar – LinkedIn</h1>")
    page = page.replace("<script>", f"<script>window.JOBS_DATA = {payload};</script>\n<script>", 1)
    out = root / "linkedin_results.html"
    out.write_text(page, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Profile-driven LinkedIn job search (local use).")
    parser.add_argument("--dry-run", action="store_true", help="only print the searches")
    parser.add_argument("--no-open", action="store_true", help="don't open the results page")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true":
        print("The LinkedIn search is for your own computer only – it doesn't run in CI.")
        return 0
    run(dry_run=args.dry_run, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
