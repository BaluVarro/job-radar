# Job Radar

A daily job tracker that reads postings straight from company career pages (no LinkedIn),
stores them in a database with a full audit trail, and publishes a searchable page.

It runs on GitHub Actions every morning, covers ~45 employers in consulting, finance,
tech, energy, government, policy research and international organisations, and filters
for entry-level analyst, data/AI, policy and data-governance roles in Budapest or remote/EU.

## How it works

```
config/companies.yaml ──► adapters ──► validation ──► SQLite (data/jobs.db) ──► outputs
  which pages to read     greenhouse     required      jobs: first_seen,          docs/jobs.json → viewer page
                          workday        fields, URL   last_seen, active          data/jobs.csv
                          hrxml          checks,       runs + per-source          RUN_REPORT.md
                          html           de-dup        audit log                  logs/runs.jsonl
                          browser
```

- **Adapters** (`jobradar/adapters/`) handle each kind of careers site: public APIs
  (Greenhouse, Workday), XML feeds (HR-XML), plain HTML pages, and JavaScript-heavy pages
  rendered in headless Chromium (Playwright).
- **Filters** (`config/filters.yaml`) mark which postings match your target roles and
  locations. Everything is stored; the filter only sets what the viewer shows by default.
- **Lifecycle tracking**: each posting gets `first_seen` / `last_seen`; when it disappears from
  its source it is marked closed — but only after a *successful* fetch, so a site outage
  never wipes jobs.

## Data quality

Scrapers break silently when a site changes. Job Radar checks for that on every run:

| Check | What it catches |
|---|---|
| Field validation | empty or implausible titles, malformed URLs |
| De-duplication | the same posting listed twice (tracking parameters stripped from URLs) |
| `dropped_to_zero` / `sharp_drop` | a source that suddenly returns far fewer jobs than last run — usually a broken scraper, so closures are suspended for that source |
| `spike` | far more "jobs" than usual — usually navigation links being picked up |
| `first_run` with 0 jobs | a new source that needs tuning |
| Run log | every run and every source's result is recorded in `data/jobs.db` and `logs/runs.jsonl` |

`RUN_REPORT.md` lists new matching postings and every source that needs attention.

## Setup

1. Open the folder in VS Code and create the repo:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: job radar"
   gh repo create job-radar --public --source=. --push
   ```
2. **Allow the workflow to save data:** on GitHub, *Settings → Actions → General →
   Workflow permissions → Read and write permissions*.
3. **Publish the viewer:** *Settings → Pages → Build and deployment → Deploy from a branch →
   `main` / `/docs`*. The page appears at `https://<your-username>.github.io/job-radar/`.
4. **First run:** *Actions → Daily job scrape → Run workflow*. After that it runs every morning.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

python -m jobradar                       # all sources
python -m jobradar --only erste,formlabs  # just a few, for testing
python -m jobradar --no-browser          # skip JavaScript-heavy sites
pytest                                   # tests
```

## Tuning a source

After the first run, open `RUN_REPORT.md` (or *Source health* on the viewer page):

- **0 jobs** → open the careers page in your browser, look at what a job link looks like
  (e.g. `/job/12345`), and set `options: {link_pattern: "/job/"}` for that company.
  If the page is blank until JavaScript loads, change `adapter: html` to `adapter: browser`.
- **spike** → the pattern is too loose; make `link_pattern` more specific or add
  `exclude_pattern`.
- **Global job lists** (e.g. McKinsey, Morgan Stanley, Nokia) → filter by Budapest on the
  site, copy the resulting URL into `url`.
- **Add a company** → add an entry to `config/companies.yaml`; disabled entries list pages
  that still need a URL.

## Responsible scraping

Job Radar reads only public careers pages, identifies itself with a descriptive
User-Agent, waits two seconds between requests and visits each source once a day.
Check a site's terms before adding it.

## Project structure

```
config/            companies.yaml (sources), filters.yaml (roles & locations)
jobradar/          the scraper package (python -m jobradar)
  adapters/        one module per kind of careers site
  quality.py       validation and anomaly checks
  storage.py       SQLite schema, lifecycle tracking, audit log
  report.py        JSON / CSV / Markdown outputs
docs/index.html    viewer page (GitHub Pages)
tests/             pytest suite with fixture pages
.github/workflows/ daily schedule
```
