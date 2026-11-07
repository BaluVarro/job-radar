"""Tests for the LinkedIn search, using saved sample pages instead of the internet."""

import json
import shutil
from pathlib import Path

import pytest

from jobradar import linkedin

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
SEARCH_HTML = (FIX / "linkedin_search.html").read_text()
JOB_HTML = (FIX / "linkedin_job.html").read_text()


class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Answers search requests with the sample results page and job requests with the
    sample job page. `refuse_after` makes LinkedIn 'block' after that many requests."""

    def __init__(self, refuse_after=None):
        self.calls = []
        self.refuse_after = refuse_after

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.refuse_after is not None and len(self.calls) > self.refuse_after:
            return FakeResponse(999)
        if "seeMoreJobPostings" in url:
            return FakeResponse(200, SEARCH_HTML)
        if url.endswith("4101234567"):
            return FakeResponse(200, JOB_HTML)
        return FakeResponse(200, "<div class='show-more-less-html__markup'>Short.</div>")


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "docs").mkdir()
    for name in ("filters.yaml", "profile.yaml"):
        shutil.copy(ROOT / "config" / name, tmp_path / "config" / name)
    shutil.copy(ROOT / "docs" / "index.html", tmp_path / "docs" / "index.html")
    # No waiting between requests in tests (the real minimum is 3 seconds).
    monkeypatch.setattr(linkedin, "MIN_DELAY", 0)
    profile = tmp_path / "config" / "profile.yaml"
    profile.write_text(profile.read_text().replace("delay_seconds: 5", "delay_seconds: 0"))
    return tmp_path


def run(project, session):
    return linkedin.run(root=project, session=session, open_browser=False)


def test_parse_search_reads_cards():
    cards = linkedin.parse_search(SEARCH_HTML)
    assert [c["job_id"] for c in cards] == ["4101234567", "4107654321", "4109999999"]
    first = cards[0]
    assert first["title"] == "Junior Data Governance Analyst"
    assert first["company"] == "Acme Bank"
    assert first["location"] == "Budapest, Budapest, Hungary"
    assert first["posted"] == "2026-11-06"
    assert first["url"] == "https://www.linkedin.com/jobs/view/4101234567"   # tracking removed


def test_parse_detail_includes_criteria_and_description():
    text = linkedin.parse_detail(JOB_HTML)
    assert text.startswith("Seniority level: Entry level")
    assert "data quality checks in Python" in text


def test_build_searches_from_profile():
    cfg = {"keywords": ["data analyst", "economist"],
           "locations": [{"name": "Budapest", "location": "Budapest, Hungary"},
                         {"name": "Remote", "location": "European Union", "remote": True}],
           "experience": ["internship", "entry"], "posted_within_days": 7}
    searches = linkedin.build_searches(cfg)
    assert len(searches) == 4
    params = searches[1]["params"]
    assert params["keywords"] == "data analyst" and params["f_WT"] == "2"
    assert params["f_E"] == "1,2" and params["f_TPR"] == "r604800"


def test_full_run_scores_and_writes_local_page(project):
    session = FakeSession()
    summary = run(project, session)
    assert summary["stopped"] is None and summary["postings"] == 3
    page = (project / "linkedin_results.html").read_text()
    assert "window.JOBS_DATA" in page and "Job Radar – LinkedIn" in page
    data = json.loads(page.split("window.JOBS_DATA = ", 1)[1].split(";</script>", 1)[0]
                      .replace("<\\/", "</"))
    jobs = {j["title"]: j for j in data["jobs"]}
    best = jobs["Junior Data Governance Analyst"]
    assert best["score"] >= 75 and best["score"] == max(j["score"] for j in data["jobs"])
    assert any("entry-level" in r for r in best["score_reasons"])
    assert jobs["Senior Tax Manager"]["score"] == 0
    assert best["queries"]                                   # which searches found it
    # Each job page is read once: a second run doesn't fetch them again.
    first_run_calls = len(session.calls)
    session2 = FakeSession()
    run(project, session2)
    assert len(session2.calls) < first_run_calls


def test_stops_immediately_when_linkedin_refuses(project):
    session = FakeSession(refuse_after=2)
    summary = run(project, session)
    assert "999" in summary["stopped"]
    assert len(session.calls) == 3            # 2 answered + the refused one, then nothing more
    assert summary["postings"] == 3           # what was found before the refusal is kept


def test_request_cap_is_respected(project):
    profile = (project / "config" / "profile.yaml").read_text()
    (project / "config" / "profile.yaml").write_text(
        profile.replace("  delay_seconds: 0", "  delay_seconds: 0\n  max_requests: 4"))
    session = FakeSession()
    summary = run(project, session)
    assert len(session.calls) == 4 and summary["capped"] is True


def test_flags_postings_already_on_careers_pages(project):
    from jobradar.models import Job
    from jobradar.storage import Store
    store = Store(project / "data" / "jobs.db")
    job = Job("acme", "Acme Bank", "Finance", "Junior Data Governance Analyst",
              "https://acme.test/job/1", "Budapest")
    store.upsert_jobs([job], {job.uid: True}, "2026-11-07")
    store.close()
    run(project, FakeSession())
    store = Store(project / "data" / "linkedin.db")
    reasons = [json.loads(j["score_reasons"]) for j in store.active_jobs()
               if j["title"] == "Junior Data Governance Analyst"][0]
    assert "+ also on the company's own careers page" in reasons


def test_refuses_to_run_in_ci(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert linkedin.main(["--no-open"]) == 0
    assert "own computer only" in capsys.readouterr().out
