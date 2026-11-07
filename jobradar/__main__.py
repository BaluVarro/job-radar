"""Run the daily scrape:  python -m jobradar  [--only id1,id2] [--no-browser]"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import quality
from .adapters import ADAPTERS
from .adapters.browser import BrowserManager
from .filters import Filters
from .http import make_session
from .report import write_outputs
from .storage import Store

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("jobradar")


def load_config(root: Path) -> tuple[list[dict], Filters]:
    companies = yaml.safe_load((root / "config" / "companies.yaml").read_text(encoding="utf-8"))
    filters = yaml.safe_load((root / "config" / "filters.yaml").read_text(encoding="utf-8"))
    return companies["companies"], Filters.from_config(filters)


def scrape_source(company, session, browser, filters, store, run_id, today):
    """Fetch one company, validate, store. Returns (source record, new uids, closed count)."""
    started = time.monotonic()
    rec = {"company_id": company["id"], "company": company["name"], "status": "ok",
           "n_found": 0, "n_valid": 0, "n_invalid": 0, "n_new": 0,
           "duration_s": 0.0, "error": None, "anomaly": None}
    adapter_cls = ADAPTERS[company["adapter"]]
    try:
        jobs = adapter_cls(company, session, browser=browser).fetch()
    except Exception as exc:  # one broken site must not stop the run
        rec.update(status="error", error=f"{type(exc).__name__}: {exc}"[:300],
                   duration_s=round(time.monotonic() - started, 1))
        log.warning("%-28s ERROR %s", company["id"], rec["error"])
        return rec, [], 0

    rec["n_found"] = len(jobs)
    unique = {}
    for job in jobs:
        problems = quality.validate(job)
        if problems:
            rec["n_invalid"] += 1
            log.debug("invalid %s: %s (%s)", company["id"], job.title, problems)
        elif job.uid not in unique:
            unique[job.uid] = job
    valid = list(unique.values())
    rec["n_valid"] = len(valid)

    # Any anomaly (incl. 0 jobs on the first run) usually means the scraper needs tuning.
    rec["anomaly"] = quality.anomaly(store.previous_valid_count(company["id"]), len(valid))
    if rec["anomaly"]:
        rec["status"] = "warn"

    matches = {j.uid: filters.matches(j) for j in valid}
    new_uids = store.upsert_jobs(valid, matches, today)
    # Don't close postings when the count collapsed: that's more likely a scraper problem.
    closed = 0 if rec["anomaly"] in ("dropped_to_zero", "sharp_drop") else \
        store.close_missing(company["id"], set(unique))
    rec["n_new"] = len(new_uids)
    rec["duration_s"] = round(time.monotonic() - started, 1)
    log.info("%-28s %-5s found=%-4d valid=%-4d new=%-3d %s", company["id"], rec["status"],
             rec["n_found"], rec["n_valid"], rec["n_new"], rec["anomaly"] or "")
    return rec, [(u, matches[u]) for u in new_uids], closed


def run(root: Path = ROOT, only: set[str] | None = None, use_browser: bool = True,
        reset: set[str] | None = None) -> dict:
    companies, filters = load_config(root)
    selected = [c for c in companies if c.get("enabled", True)
                and (not only or c["id"] in only)]
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y-%m-%dT%H%M%SZ")
    today = started.date().isoformat()

    store = Store(root / "data" / "jobs.db")
    for company_id in reset or ():
        log.info("Reset %s: deleted %d stored postings", company_id, store.reset_source(company_id))
    session = make_session()
    needs_browser = use_browser and any(c["adapter"] == "browser" for c in selected)
    browser = BrowserManager() if needs_browser else None

    sources, all_new, n_closed = [], [], 0
    try:
        for company in selected:
            rec, new, closed = scrape_source(company, session, browser, filters, store,
                                             run_id, today)
            store.record_source_run(run_id, rec)
            sources.append(rec)
            all_new += new
            n_closed += closed
    finally:
        if browser:
            browser.close()

    summary = {
        "run_id": run_id,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_sources": len(sources),
        "n_ok": sum(s["status"] == "ok" for s in sources),
        "n_warn": sum(s["status"] == "warn" for s in sources),
        "n_failed": sum(s["status"] == "error" for s in sources),
        "n_new": len(all_new),
        "n_new_matching": sum(m for _, m in all_new),
        "n_closed": n_closed,
    }
    store.record_run(summary)
    write_outputs(root, summary, sources, store.active_jobs(), {u for u, _ in all_new})
    store.close()
    log.info("Done: %d new postings (%d matching), %d failed sources",
             summary["n_new"], summary["n_new_matching"], summary["n_failed"])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape company career pages into a job database.")
    parser.add_argument("--only", help="comma-separated company ids to run (for testing)")
    parser.add_argument("--no-browser", action="store_true",
                        help="skip JavaScript-heavy sites (no Playwright needed)")
    parser.add_argument("--reset", help="comma-separated company ids whose stored postings and "
                        "history are deleted before the run (use after re-tuning a source)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    only = set(args.only.split(",")) if args.only else None
    reset = {r.strip() for r in args.reset.split(",") if r.strip()} if args.reset else None
    summary = run(only=only, use_browser=not args.no_browser, reset=reset)
    # Fail the GitHub Action (so you get an email) only if every source failed.
    return 1 if summary["n_sources"] and summary["n_failed"] == summary["n_sources"] else 0


if __name__ == "__main__":
    sys.exit(main())
