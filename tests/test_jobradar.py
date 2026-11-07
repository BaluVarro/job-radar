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
    shutil.copy(ROOT / "config" / "profile.yaml", tmp_path / "config" / "profile.yaml")
    (tmp_path / "config" / "companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"id": "a", "name": "Alpha", "category": "Test", "adapter": "fake", "url": "https://a.test"},
        {"id": "b", "name": "Beta", "category": "Test", "adapter": "fake", "url": "https://b.test"},
    ]}))
    monkeypatch.setitem(runner.ADAPTERS, "fake", FakeAdapter)
    # Job pages are "read" from this dict instead of the internet.
    monkeypatch.setattr(runner, "fetch_description", lambda session, url: FAKE_PAGES.get(url, ""))
    return tmp_path


FAKE_PAGES: dict = {}


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


def test_changing_a_sources_settings_clears_its_old_junk(project):
    """Re-tuning a source (e.g. Political Capital) must close the junk it stored before,
    without a manual reset, instead of being flagged as a 'sharp_drop'."""
    junk = [(f"Political Capital news {i}", f"https://a.test/news/{i}", "Budapest") for i in range(30)]
    FakeAdapter.responses = {"a": junk, "b": []}
    runner.run(root=project, use_browser=False)

    cfg_path = project / "config" / "companies.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["companies"][0]["options"] = {"title_pattern": "Álláshirdetés"}   # the re-tune
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    FakeAdapter.responses = {"a": [("Elemző Álláshirdetés", "https://a.test/news/99", "Budapest")],
                             "b": []}
    summary = runner.run(root=project, use_browser=False)

    assert summary["n_closed"] == 30
    data = json.loads((project / "docs" / "jobs.json").read_text())
    assert [j["title"] for j in data["jobs"] if j["company_id"] == "a"] == ["Elemző Álláshirdetés"]
    health = json.loads((project / "docs" / "health.json").read_text())
    assert next(s for s in health["sources"] if s["company_id"] == "a")["anomaly"] is None


def test_unchanged_settings_still_protect_against_broken_scrapes(project):
    many = [(f"Policy Analyst {i}", f"https://a.test/job/{i}", "Budapest") for i in range(30)]
    FakeAdapter.responses = {"a": many, "b": []}
    runner.run(root=project, use_browser=False)
    FakeAdapter.responses = {"a": many[:2], "b": []}          # same settings, count collapses
    summary = runner.run(root=project, use_browser=False)
    assert summary["n_closed"] == 0                         # nothing closed: likely a broken scrape
    health = json.loads((project / "docs" / "health.json").read_text())
    assert next(s for s in health["sources"] if s["company_id"] == "a")["anomaly"] == "sharp_drop"


# --- scoring --------------------------------------------------------------------------

from jobradar.scorer import Scorer, extract_text  # noqa: E402


def scorer():
    return Scorer(yaml.safe_load((ROOT / "config" / "profile.yaml").read_text()))


GOOD = """We are looking for a Junior Data Governance Analyst to join our Budapest team.
You will work with Python and pandas to build data quality checks, automate reporting and
support our data management framework. Knowledge of machine learning or NLP is an advantage.
German language skills are a plus. Fresh graduates welcome."""

BAD = """Senior Tax Manager. Minimum 7 years of experience in corporate tax advisory.
Fluent French is required. Location: Paris."""


def test_good_fit_scores_high_and_explains_why():
    s = scorer().score("Junior Data Governance Analyst", GOOD, "Budapest", "https://x.test/job/1")
    assert s.total >= 75
    text = " ".join(s.reasons)
    assert "data governance" in text and "Python" in text and "entry-level" in text
    assert "Budapest" in text and "German valued" in text


def test_bad_fit_scores_low_with_reasons():
    s = scorer().score("Senior Tax Manager", BAD, "Paris", "https://x.test/job/2")
    assert s.total <= 10
    text = " ".join(s.reasons)
    assert "7+ years experience" in text and "requires French" in text and "field you excluded" in text


def test_optional_language_is_not_penalised():
    s = scorer().score("Data Analyst", "Python and SQL. French is an advantage.", "Budapest")
    assert not any("French" in r for r in s.reasons)


def test_title_only_when_page_not_read():
    s = scorer().score("Policy Analyst", "", "Budapest")
    assert s.total > 0 and any("title only" in r for r in s.reasons)


def test_hungarian_posting():
    desc = "Pályakezdő adatelemző munkatársat keresünk. Elvárás: Python, statisztikai ismeretek."
    s = scorer().score("Gyakornok – adatelemző", desc, "Budapest")
    assert s.total >= 50 and any("entry-level" in r for r in s.reasons)


def test_extract_text_drops_menus_and_scripts():
    html = ("<html><body><nav>Home Jobs Login</nav><main><h1>Data Analyst</h1>"
            "<p>Work with Python.</p><script>var x=1</script></main><footer>© Acme</footer></body></html>")
    text = extract_text(html)
    assert "Data Analyst" in text and "Python" in text
    assert "Login" not in text and "var x" not in text and "Acme" not in text


def test_run_scores_postings_and_sorts_by_score(project):
    FAKE_PAGES.clear()
    FAKE_PAGES["https://a.test/job/1"] = GOOD
    FakeAdapter.responses = {
        "a": [("Junior Data Governance Analyst", "https://a.test/job/1", "Budapest"),
              ("Policy Intern", "https://a.test/job/2", "Budapest")],
        "b": [("Research Assistant", "https://b.test/job/3", "Remote")]}
    summary = runner.run(root=project, use_browser=False)
    assert summary["n_scored"] == 3 and summary["n_described"] == 1
    data = json.loads((project / "docs" / "jobs.json").read_text())
    scores = [j["score"] for j in data["jobs"]]
    assert scores == sorted(scores, reverse=True)
    top = data["jobs"][0]
    assert top["title"] == "Junior Data Governance Analyst" and top["score"] >= 75
    assert isinstance(top["score_reasons"], list) and top["score_reasons"]
    assert "Top 15 open postings by score" in (project / "RUN_REPORT.md").read_text()
    assert "score" in (project / "data" / "jobs.csv").read_text().splitlines()[0]


# --- experience that's implied rather than stated --------------------------------------

def test_implied_experience_is_penalised():
    desc = ("Data Analyst to join our Budapest team. You have a proven track record of delivering "
            "analytics projects and extensive experience with stakeholder management. Python, SQL. "
            "We offer a friendly team and hybrid work.")
    s = scorer().score("Data Analyst", desc, "Budapest")
    text = " ".join(s.reasons)
    assert "implies experience" in text and "proven track record" in text
    plain = scorer().score("Data Analyst", desc.replace("You have a proven track record of delivering "
                           "analytics projects and extensive experience with stakeholder management. ", ""),
                           "Budapest")
    assert s.total <= plain.total - 20


def test_written_numbers_of_years():
    s = scorer().score("Policy Analyst", "At least five years of relevant experience required. " * 3,
                       "Budapest")
    assert any("5+ years experience" in r for r in s.reasons)
    hu = scorer().score("Elemző", "Legalább három év szakmai tapasztalat szükséges. " * 5, "Budapest")
    assert any("3 years experience" in r for r in hu.reasons)


def test_linkedin_mid_senior_level_is_penalised():
    desc = "Seniority level: Mid-Senior level\nEmployment type: Full-time\n" + GOOD
    s = scorer().score("Data Governance Analyst", desc, "Budapest")
    assert any("LinkedIn level: Mid-Senior level" in r for r in s.reasons)
    assert s.total < scorer().score("Data Governance Analyst", GOOD, "Budapest").total - 20


def test_title_only_score_is_capped():
    s = scorer().score("Junior Data Governance Analyst", "", "Budapest")
    assert s.total == 45 and any("title only" in r for r in s.reasons)
