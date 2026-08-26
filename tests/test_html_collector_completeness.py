"""Completeness for the four HTML-paginating collectors.

iCIMS and SuccessFactors carried the tightest ceilings in the codebase - 10
pages x ~20 rows and 8 pages x 25 rows, so roughly 200 jobs each, with no
reconciliation against anything the page reported. Avature and Radancy walk
until a page yields no new rows, which is a correct end marker but was
indistinguishable from a page that failed.

The four remaining collectors (Greenhouse, Lever, Ashby, Jobvite) return their
entire board in one response and stay on the bare-list shim deliberately:
there is no pagination for them to get wrong.
"""


import ats.avature as avature_module
import ats.icims as icims_module
import ats.radancy as radancy_module
import ats.successfactors as sf_module
from ats.avature import AvatureCollector
from ats.base import STOP_BUDGET, CollectionResult
from ats.icims import ICIMSCollector
from ats.radancy import RadancyCollector
from ats.successfactors import SuccessFactorsCollector


def _rows_html(start: int, count: int, href_tmpl: str) -> str:
    links = "".join(
        f'<a href="{href_tmpl.format(i=i)}">Data Engineer {i}</a>'
        for i in range(start, start + count)
    )
    return f"<html><body>{links}</body></html>"


# --- iCIMS -----------------------------------------------------------------

def _icims(monkeypatch, total, page_size=20):
    def fake(url, params=None, **kw):
        start = params["pr"] * page_size
        n = max(0, min(page_size, total - start))
        return _rows_html(start, n, "/jobs/{i}/data-engineer/job")
    monkeypatch.setattr(icims_module.http_client, "get_text", fake)
    return ICIMSCollector("PepsiCo", {
        "provider": "icims", "host": "pepjobs-pepsico.icims.com",
        "tenant": "pepsico", "url": "https://pepjobs-pepsico.icims.com/jobs/search"})


def test_icims_walks_past_the_old_two_hundred_job_ceiling(monkeypatch):
    result = _icims(monkeypatch, total=640).collect()
    assert isinstance(result, CollectionResult)
    assert len(result.jobs) == 640, "still capped near the old 10-page limit"


def test_icims_marks_a_budget_trip_incomplete(monkeypatch):
    collector = _icims(monkeypatch, total=9000)
    monkeypatch.setattr(type(collector), "max_jobs", property(lambda self: 100))
    result = collector.collect()
    assert result.complete is False
    assert result.stop_reason == STOP_BUDGET


# --- SuccessFactors --------------------------------------------------------

def _successfactors(monkeypatch, total, page_size=25):
    def fake(url, params=None, **kw):
        start = params["startrow"]
        n = max(0, min(page_size, total - start))
        return _rows_html(start, n, "/job/{i}/data-engineer")
    monkeypatch.setattr(sf_module.http_client, "get_text", fake)
    return SuccessFactorsCollector("HCLTech", {
        "provider": "successfactors", "host": "career55.sapsf.eu",
        "tenant": "career55", "url": "https://career55.sapsf.eu/careers"})


def test_successfactors_walks_past_the_old_two_hundred_job_ceiling(monkeypatch):
    result = _successfactors(monkeypatch, total=500).collect()
    assert len(result.jobs) == 500, "still capped near the old 8-page limit"


def test_successfactors_marks_a_budget_trip_incomplete(monkeypatch):
    collector = _successfactors(monkeypatch, total=9000)
    monkeypatch.setattr(type(collector), "max_jobs", property(lambda self: 75))
    result = collector.collect()
    assert result.complete is False
    assert result.stop_reason == STOP_BUDGET


# --- Avature ---------------------------------------------------------------

def _avature(monkeypatch, total, page_size=100):
    def fake(url, params=None, **kw):
        start = params["jobOffset"]
        n = max(0, min(page_size, total - start))
        return _rows_html(start, n, "/careers/JobDetail/{i}")
    monkeypatch.setattr(avature_module.http_client, "get_text", fake)
    return AvatureCollector("Deloitte", {
        "provider": "avature", "host": "deloitte.avature.net",
        "tenant": "deloitte", "url": "https://deloitte.avature.net/careers"})


def test_avature_returns_a_collection_result(monkeypatch):
    result = _avature(monkeypatch, total=250).collect()
    assert isinstance(result, CollectionResult)
    assert len(result.jobs) == 250
    assert result.complete is True


# --- Radancy ---------------------------------------------------------------

def _radancy(monkeypatch, total, page_size=500):
    def fake(url, params=None, **kw):
        page = params["CurrentPage"]
        start = (page - 1) * page_size
        n = max(0, min(page_size, total - start))
        cards = "".join(
            f'<a data-job-id="{i}" href="/job/dallas/de/1/{i}"><h2>Data Engineer {i}</h2></a>'
            for i in range(start, start + n)
        )
        return f'{{"results": "{cards}"}}'.replace('"<a', '"<a')
    monkeypatch.setattr(radancy_module.http_client, "get_text", fake)
    return RadancyCollector("7-Eleven", {
        "provider": "radancy", "host": "careers.7-eleven.com",
        "url": "https://careers.7-eleven.com/search-jobs"})


def test_radancy_returns_a_collection_result(monkeypatch):
    result = _radancy(monkeypatch, total=300).collect()
    assert isinstance(result, CollectionResult)
    assert result.complete is True
