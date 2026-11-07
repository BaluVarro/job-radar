"""SQLite storage with a full audit trail of runs.

Tables:
  jobs         one row per posting; first_seen / last_seen / active track its life
  runs         one row per daily run
  source_runs  one row per company per run: status, counts, errors, anomaly flags
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    company TEXT NOT NULL,
    category TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT,
    posted TEXT,
    source TEXT,
    match INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started TEXT, finished TEXT,
    n_sources INTEGER, n_ok INTEGER, n_warn INTEGER, n_failed INTEGER,
    n_new INTEGER, n_new_matching INTEGER, n_closed INTEGER
);
CREATE TABLE IF NOT EXISTS source_runs (
    run_id TEXT, company_id TEXT, company TEXT,
    status TEXT,              -- ok | warn | error
    n_found INTEGER, n_valid INTEGER, n_invalid INTEGER, n_new INTEGER,
    duration_s REAL, error TEXT, anomaly TEXT,
    config_hash TEXT,         -- fingerprint of the source's settings for this run
    PRIMARY KEY (run_id, company_id)
);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Databases created by older versions lack the config_hash column: add it.
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(source_runs)")}
        if "config_hash" not in cols:
            self.db.execute("ALTER TABLE source_runs ADD COLUMN config_hash TEXT")

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # --- jobs -------------------------------------------------------------
    def upsert_jobs(self, jobs: list[Job], matches: dict[str, bool], today: str) -> list[str]:
        """Insert new postings, refresh existing ones. Returns uids of new postings."""
        new = []
        for job in jobs:
            row = self.db.execute("SELECT uid FROM jobs WHERE uid = ?", (job.uid,)).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO jobs (uid, company_id, company, category, title, url, location,"
                    " posted, source, match, first_seen, last_seen, active)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (job.uid, job.company_id, job.company, job.category, job.title, job.url,
                     job.location, job.posted, job.source, int(matches[job.uid]), today, today))
                new.append(job.uid)
            else:
                self.db.execute(
                    "UPDATE jobs SET title=?, location=?, posted=?, match=?, last_seen=?, active=1"
                    " WHERE uid=?",
                    (job.title, job.location, job.posted, int(matches[job.uid]), today, job.uid))
        return new

    def close_missing(self, company_id: str, seen_uids: set[str]) -> int:
        """Mark postings that disappeared from a source as closed.
        Only called after a successful fetch, so a failed scrape never closes jobs."""
        rows = self.db.execute(
            "SELECT uid FROM jobs WHERE company_id = ? AND active = 1", (company_id,)).fetchall()
        gone = [r["uid"] for r in rows if r["uid"] not in seen_uids]
        self.db.executemany("UPDATE jobs SET active = 0 WHERE uid = ?", [(u,) for u in gone])
        return len(gone)

    def reset_source(self, company_id: str) -> int:
        """Forget everything stored for one source (after re-tuning its scraper).
        Deletes its postings and its run history, so the next run starts fresh and the
        anomaly checks don't compare against the old, wrong counts. Returns rows deleted."""
        deleted = self.db.execute("DELETE FROM jobs WHERE company_id = ?", (company_id,)).rowcount
        self.db.execute("DELETE FROM source_runs WHERE company_id = ?", (company_id,))
        self.db.commit()
        return deleted

    def active_jobs(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE active = 1 ORDER BY first_seen DESC, company, title")
        return [dict(r) for r in rows]

    # --- runs -------------------------------------------------------------
    def previous_valid_count(self, company_id: str, config_hash: str | None = None) -> int | None:
        """Job count from the last successful run of this source with the same settings."""
        row = self.db.execute(
            "SELECT n_valid FROM source_runs WHERE company_id = ? AND status != 'error'"
            " AND config_hash IS ? ORDER BY run_id DESC LIMIT 1",
            (company_id, config_hash)).fetchone()
        return None if row is None else row["n_valid"]

    def has_history(self, company_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM source_runs WHERE company_id = ? LIMIT 1",
                               (company_id,)).fetchone() is not None

    def record_source_run(self, run_id: str, rec: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO source_runs (run_id, company_id, company, status, n_found,"
            " n_valid, n_invalid, n_new, duration_s, error, anomaly, config_hash)"
            " VALUES (:run_id,:company_id,:company,:status,:n_found,:n_valid,:n_invalid,:n_new,"
            " :duration_s,:error,:anomaly,:config_hash)",
            {"run_id": run_id, "config_hash": None, **rec})

    def record_run(self, rec: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (:run_id,:started,:finished,:n_sources,:n_ok,"
            ":n_warn,:n_failed,:n_new,:n_new_matching,:n_closed)", rec)
        self.db.commit()
