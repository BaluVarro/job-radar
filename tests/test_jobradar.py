import json
import shutil
from pathlib import Path

import pytest
import yaml

from jobradar import __main__ as runner
from jobradar import quality
from jobradar.adapters.greenhouse import GreenhouseAdapter
from jobradar.adapters.hrxml import HrXmlAdapter
from jobradar.adapters.html import HtmlAdapter
from jobradar.filters import Filters
from jobradar.models import Job, normalize_url

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


def company(**kw):
    base = {"id": "acme", "name": "Acme", "category": "Test", "url": "https://acme.test/careers"}
    base.update(kw)
    return base


def filters():
    return Filters.from_config(yaml.safe_load((ROOT / "config" / "filters.yaml").read_text()))


# --- adapters -----------------------------------------------------------------

def test_html_adapter_keeps_job_links_and_drops_navigation():
    jobs = HtmlAdapter(company(options={"link_pattern": "/job/"}), None).parse(
        (FIX / "careers.html").read_text(), base_url="https://acme.test/careers")
    titles = [j.title for j in jobs]
    assert "Data Analyst" in titles
    assert "Senior Data Engineer" in titles
    assert "Apply now" not in titles                       # navigation text dropped
    assert all("login" not in j.url for j in jobs)
    analyst = next(j for j in jobs if j.title == "Data Analyst")
    assert analyst.url == "https://acme.test/job/Budapest-Data-Analyst/123456/"
    assert analyst.location == "Budapest, Hungary"          # picked up from the list item


def test_html_adapter_default_pattern_without_options():
    jobs = HtmlAdapter(company(), None).parse((FIX / "careers.html").read_text())
    assert {"Data Analyst", "Policy Analyst"} <= {j.title for j in jobs}


def test_hrxml_adapter_parses_feed():
    jobs = HrXmlAdapter(company(default_location="Hungary"), None).parse(
        (FIX / "feed.xml").read_bytes())
    assert [j.title for j in jobs] == ["Junior adatelemző", "Erste Mobilbankár - Tolna megye"]
    assert jobs[0].location == "Budapest" and jobs[0].posted == "2026-11-02"
    assert jobs[1].location == "Hungary"                    # falls back to default_location


def test_greenhouse_adapter_parses_api():
    jobs = GreenhouseAdapter(company(options={"board_token": "x"}), None).parse(
        json.loads((FIX / "greenhouse.json").read_text()))
    assert jobs[0].title == "Data Scientist Intern"
    assert jobs[0].location == "Budapest, Hungary" and jobs[0].posted == "2026-11-01"


# --- filters & quality ----------------------------------------------------------

@pytest.mark.parametrize("title,location,expected", [
    ("Data Analyst", "Budapest, Hungary", True),
    ("Junior adatelemző", "Budapest", True),
    ("Senior Data Engineer", "Budapest", False),            # excluded: senior
    ("Policy Analyst", "Warsaw, Poland", False),            # wrong location
    ("Mechanical Engineer", "Budapest", False),             # not a target role
    ("Graduate Programme 2027", "Remote - EMEA", True),
    ("Schuman Traineeship", "EU", True),
    ("Feladatkoordinátor", "Budapest", False),              # 'adat' inside a word must not match
])
def test_filters(title, location, expected):
    job = Job("a", "A", "T", title, "https://a.test/job/1", location)
    assert filters().matches(job) is expected


def test_validate_flags_bad_postings():
    assert quality.validate(Job("a", "A", "T", "Data Analyst", "https://a.test/j/1")) == []
    assert "invalid url" in quality.validate(Job("a", "A", "T", "Data Analyst", "/j/1"))
    assert quality.validate(Job("a", "A", "T", "", "https://a.test/j/1")) == ["missing title"]


@pytest.mark.parametrize("prev,cur,expected", [
    (None, 5, None), (None, 0, "first_run"), (10, 0, "dropped_to_zero"),
    (40, 5, "sharp_drop"), (5, 200, "spike"), (10, 9, None),
])
def test_anomaly(prev, cur, expected):
    assert quality.anomaly(prev, cur) == expected


def test_normalize_url_strips_tracking_and_fragments():
    assert normalize_url("HTTPS://A.test/job/1/?utm_source=x&id=2#top") == "https://a.test/job/1?id=2"


# --- end-to-end run with fake sources ------------------------------------------------

class FakeAdapter:
    responses: dict = {}

    def __init__(self, company, session, **context):
        self.company = company

    def fetch(self):
        result = self.responses[self.company["id"]]
        if isinstance(result, Exception):
            raise result
        return [Job(self.company["id"], self.company["name"], "Test", t, u, loc, source="fake")
                for t, u, loc in result]


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "filters.yaml", tmp_path / "config" / "filters.yaml")
    (tmp_path / "config" / "companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"id": "a", "name": "Alpha", "category": "Test", "adapter": "fake", "url": "https://a.test"},
        {"id": "b", "name": "Beta", "category": "Test", "adapter": "fake", "url": "https://b.test"},
    ]}))
    monkeypatch.setitem(runner.ADAPTERS, "fake", FakeAdapter)
    return tmp_path


def test_full_run_tracks_new_closed_and_failures(project):
    a_jobs = [("Data Analyst", "https://a.test/job/1", "Budapest"),
              ("Policy Intern", "https://a.test/job/2", "Budapest"),
              ("Office Cleaner", "https://a.test/job/3", "Budapest")]
    FakeAdapter.responses = {"a": a_jobs, "b": [("Junior Economist", "https://b.test/job/9", "Remote")]}
    first = runner.run(root=project, use_browser=False)
    assert first["n_new"] == 4 and first["n_new_matching"] == 3 and first["n_failed"] == 0

    # Day 2: one Alpha job disappears, Beta's site is down.
    FakeAdapter.responses = {"a": a_jobs[:2], "b": ConnectionError("site down")}
    second = runner.run(root=project, use_browser=False)
    assert second["n_new"] == 0 and second["n_closed"] == 1 and second["n_failed"] == 1

    data = json.loads((project / "docs" / "jobs.json").read_text())
    titles = {j["title"] for j in data["jobs"]}
    assert "Office Cleaner" not in titles                 # closed
    assert "Junior Economist" in titles                   # kept: a failed fetch never closes jobs
    report = (project / "RUN_REPORT.md").read_text()
    assert "Beta" in report and "site down" in report
    assert len((project / "logs" / "runs.jsonl").read_text().splitlines()) == 2
    assert (project / "data" / "jobs.csv").exists()


def test_real_config_is_valid():
    companies = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text())["companies"]
    ids = [c["id"] for c in companies]
    assert len(ids) == len(set(ids))
    for c in companies:
        assert c["adapter"] in runner.ADAPTERS
        if c.get("enabled", True):
            assert c["url"].startswith("https://"), c["id"]


# --- options added after the first live run -------------------------------------------

NEWS_PAGE = """
<a href="hireink.php?article_read=1&article_id=3695">A Political Capital projektkoordinátor munkatársat keres Álláshirdetés</a>
<a href="hireink.php?article_read=1&article_id=3692">AI-alapú manipuláció Elemzés</a>
<a href="hireink.php?article_read=1&article_id=3620">Gyakornoki program 2026 Gyakornoki program</a>
<a href="hireink.php?article_read=1&article_id=2340">Gyakornoki program 2019. tavasz - nyár</a>
<a href="/en/call/">Olvass tovább: Call for Applications: Future Leaders Program</a>
"""


def test_news_page_keeps_only_recent_job_ads():
    opts = {"link_pattern": "article_read",
            "title_pattern": "Álláshirdetés|munkatársat keres|Gyakornoki program",
            "min_url_number": 3600}
    jobs = HtmlAdapter(company(url="https://pc.test/hireink.php", options=opts), None).parse(NEWS_PAGE)
    assert [j.title for j in jobs] == [
        "A Political Capital projektkoordinátor munkatársat keres Álláshirdetés",
        "Gyakornoki program 2026 Gyakornoki program"]


def test_title_cut_and_read_more_prefix():
    html = ('<a href="/job/1">Payroll Associate Location: Budapest Team: HR</a>'
            '<a href="/call/2">Olvass tovább: Call for Applications</a>')
    jobs = HtmlAdapter(company(options={"link_pattern": "/job/|/call/", "title_cut": " Location:"}),
                       None).parse(html)
    assert [j.title for j in jobs] == ["Payroll Associate", "Call for Applications"]


def test_privacy_notices_are_not_matched():
    job = Job("a", "A", "T", "Adatkezelési tájékoztató", "https://a.test/job/1", "Budapest")
    assert filters().matches(job) is False


def test_reset_source_wipes_postings_and_history(project):
    FakeAdapter.responses = {"a": [("Data Analyst", "https://a.test/job/1", "Budapest")] * 1
                             + [(f"Policy Analyst {i}", f"https://a.test/job/{i+10}", "Budapest")
                                for i in range(20)],
                             "b": []}
    runner.run(root=project, use_browser=False)
    # Re-tuned scraper now returns only 1 job: without a reset this is a "sharp_drop".
    FakeAdapter.responses = {"a": [("Data Analyst", "https://a.test/job/1", "Budapest")], "b": []}
    runner.run(root=project, use_browser=False, reset={"a"})
    data = json.loads((project / "docs" / "jobs.json").read_text())
    assert [j["title"] for j in data["jobs"] if j["company_id"] == "a"] == ["Data Analyst"]
    health = json.loads((project / "docs" / "health.json").read_text())
    alpha = next(s for s in health["sources"] if s["company_id"] == "a")
    assert alpha["anomaly"] is None and alpha["status"] == "ok"
