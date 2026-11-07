"""Outputs written after each run.

  docs/jobs.json     active postings, read by the viewer page (docs/index.html)
  docs/health.json   per-source status of the latest run
  data/jobs.csv      active postings, for Excel / pandas
  logs/runs.jsonl    one line per run, appended: the audit trail
  RUN_REPORT.md      human-readable summary of the latest run
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

CSV_FIELDS = ["score", "first_seen", "company", "category", "title", "location", "url", "match",
              "score_reasons", "posted", "source", "last_seen", "uid"]

TOP_N = 15


def write_outputs(root: Path, run: dict, sources: list[dict], jobs: list[dict],
                  new_uids: set[str]) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)

    for j in jobs:
        j["is_new"] = j["uid"] in new_uids
        reasons = j.get("score_reasons")
        j["score_reasons"] = json.loads(reasons) if isinstance(reasons, str) else (reasons or [])

    (root / "docs" / "jobs.json").write_text(json.dumps(
        {"generated": run["finished"], "run_id": run["run_id"], "jobs": jobs},
        ensure_ascii=False, indent=1), encoding="utf-8")

    (root / "docs" / "health.json").write_text(json.dumps(
        {"run": run, "sources": sources}, ensure_ascii=False, indent=1), encoding="utf-8")

    with open(root / "data" / "jobs.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**j, "score_reasons": " · ".join(j["score_reasons"])} for j in jobs)

    with open(root / "logs" / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({**run, "sources": sources}, ensure_ascii=False) + "\n")

    (root / "RUN_REPORT.md").write_text(render_markdown(run, sources, jobs), encoding="utf-8")


def _score(j: dict) -> int:
    return j.get("score") if j.get("score") is not None else -1


def _job_table(rows: list[dict]) -> list[str]:
    lines = ["| Score | Company | Title | Why |", "|---|---|---|---|"]
    for j in rows:
        title = j["title"].replace("|", "/")
        why = " · ".join(j.get("score_reasons") or []).replace("|", "/")
        score = j["score"] if j.get("score") is not None else "–"
        lines.append(f"| {score} | {j['company']} | [{title}]({j['url']}) | {why} |")
    return lines


def render_markdown(run: dict, sources: list[dict], jobs: list[dict]) -> str:
    new_matching = sorted((j for j in jobs if j.get("is_new") and j["match"]),
                          key=lambda j: -_score(j))
    top = sorted((j for j in jobs if j["match"]), key=lambda j: -_score(j))[:TOP_N]
    lines = [
        f"# Job radar – run {run['run_id']}",
        "",
        f"- Sources checked: **{run['n_sources']}** "
        f"(ok {run['n_ok']}, warnings {run['n_warn']}, failed {run['n_failed']})",
        f"- New postings: **{run['n_new']}** (matching your filters: **{run['n_new_matching']}**)",
        f"- Postings closed since last run: {run['n_closed']}",
        f"- Job pages read for scoring: {run.get('n_described', 0)}",
        "",
        f"## Top {TOP_N} open postings by score",
        "",
    ]
    lines += _job_table(top) if top else ["_No matching postings._"]
    lines += ["", "## New matching postings", ""]
    lines += _job_table(new_matching) if new_matching else ["_None this run._"]

    problems = [s for s in sources if s["status"] != "ok"]
    lines += ["", "## Sources needing attention", ""]
    if problems:
        lines += ["| Company | Status | Found | Issue |", "|---|---|---|---|"]
        for s in problems:
            issue = (s.get("error") or s.get("anomaly") or "").replace("|", "/")[:120]
            lines.append(f"| {s['company']} | {s['status']} | {s['n_valid']} | {issue} |")
    else:
        lines.append("_All sources OK._")
    return "\n".join(lines) + "\n"
