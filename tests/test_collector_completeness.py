"""The shared completeness contract across every paginating collector.

Eight collectors independently implemented the same defect: a page beyond the
first fails, the loop logs a warning and ``break``s, and the partial harvest is
returned as though it were the whole job list. Each is exercised here against
the same three-part contract:

* a walk that completes normally is ``complete=True``;
* a walk cut short by a failed page is ``complete=False`` with
  :data:`STOP_PAGE_FAILED`;
* a walk cut short by the job budget is ``complete=False`` with
  :data:`STOP_BUDGET`.

Fakes are per-collector because each provider's request/response shape differs;
the assertions are identical on purpose.
"""

import pytest

import ats.cornerstone as cornerstone_module
import ats.eightfold as eightfold_module
import ats.jibe as jibe_module
import ats.phenom as phenom_module
import ats.smartrecruiters as sr_module
import ats.ukg as ukg_module
from ats.base import STOP_BUDGET, STOP_PAGE_FAILED, CollectionResult
from ats.cornerstone import CornerstoneCollector
from ats.eightfold import EightfoldCollector
from ats.jibe import JibeCollector
from ats.phenom import PhenomCollector
from ats.smartrecruiters import SmartRecruitersCollector
from ats.ukg import UKGCollector


# --- SmartRecruiters -------------------------------------------------------

def _sr(monkeypatch, total, fail_at=None):
    def fake(url, params=None, **kw):
        offset = params["offset"]
        if fail_at is not None and offset >= fail_at:
            raise RuntimeError("HTTP 502")
        rows = [{"id": f"p{i}", "name": f"Data Engineer {i}",
                 "location": {"city": "Dallas", "region": "TX", "country": "us"}}
                for i in range(offset, min(offset + sr_module.PAGE_SIZE, total))]
        return {"totalFound": total, "content": rows}
    monkeypatch.setattr(sr_module.http_client, "get_json", fake)
    return SmartRecruitersCollector("AECOM", {
        "provider": "smartrecruiters", "url": "https://careers.smartrecruiters.com/AECOM",
        "identifier": "AECOM", "tenant": "AECOM"})


def test_smartrecruiters_full_walk_is_complete(monkeypatch):
    result = _sr(monkeypatch, total=150).collect()
    assert isinstance(result, CollectionResult)
    assert result.complete is True
    assert len(result.jobs) == 150


def test_smartrecruiters_failed_page_is_incomplete(monkeypatch):
    result = _sr(monkeypatch, total=900, fail_at=200).collect()
    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED
    assert len(result.jobs) == 200


def test_smartrecruiters_budget_trip_is_incomplete(monkeypatch):
    collector = _sr(monkeypatch, total=9000)
    monkeypatch.setattr(type(collector), "max_jobs", property(lambda self: 300))
    result = collector.collect()
    assert result.complete is False
    assert result.stop_reason == STOP_BUDGET


# --- Eightfold -------------------------------------------------------------

def _ef(monkeypatch, total, fail_at=None):
    def fake(url, params=None, **kw):
        start = params["start"]
        if fail_at is not None and start >= fail_at:
            raise RuntimeError("HTTP 503")
        rows = [{"name": f"Data Engineer {i}", "location": "Dallas, TX",
                 "canonicalPositionUrl": f"https://x.test/job/{i}"}
                for i in range(start, min(start + eightfold_module.PAGE_SIZE, total))]
        return {"count": total, "positions": rows}
    monkeypatch.setattr(eightfold_module.http_client, "get_json", fake)
    return EightfoldCollector("Acme", {
        "provider": "eightfold", "url": "https://acme.eightfold.ai/careers",
        "host": "acme.eightfold.ai", "tenant": "acme.com"})


def test_eightfold_full_walk_is_complete(monkeypatch):
    result = _ef(monkeypatch, total=80).collect()
    assert result.complete is True
    assert len(result.jobs) == 80


def test_eightfold_failed_page_is_incomplete(monkeypatch):
    result = _ef(monkeypatch, total=900, fail_at=100).collect()
    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED


# --- UKG -------------------------------------------------------------------

def _ukg(monkeypatch, total, fail_at=None):
    def fake(url, payload, **kw):
        skip = payload["opportunitySearch"]["Skip"]
        if fail_at is not None and skip >= fail_at:
            raise RuntimeError("HTTP 500")
        # Ids start at 1: UKG's _job_url uses `Id or OpportunityId`, so a
        # zero id would be treated as absent and drop the row.
        rows = [{"Title": f"Data Engineer {i}", "Id": i + 1,
                 "Locations": [{"LocalizedDescription": "Dallas, TX"}]}
                for i in range(skip, min(skip + ukg_module.PAGE_SIZE, total))]
        return {"totalCount": total, "opportunities": rows}
    monkeypatch.setattr(ukg_module.http_client, "post_json", fake)
    return UKGCollector("GameStop", {
        "provider": "ukg", "url": "https://gamestop.rec.pro.ukg.net/GAM1/JobBoard/abc",
        "host": "gamestop.rec.pro.ukg.net", "tenant": "GAM1", "site": "abc"})


def test_ukg_full_walk_is_complete(monkeypatch):
    result = _ukg(monkeypatch, total=250).collect()
    assert result.complete is True
    assert len(result.jobs) == 250


def test_ukg_failed_page_is_incomplete(monkeypatch):
    result = _ukg(monkeypatch, total=5000, fail_at=200).collect()
    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED


def test_ukg_budget_trip_is_incomplete(monkeypatch):
    """GameStop returned exactly 2,500 live - the old 100x25 ceiling."""
    collector = _ukg(monkeypatch, total=9000)
    monkeypatch.setattr(type(collector), "max_jobs", property(lambda self: 2500))
    result = collector.collect()
    assert result.complete is False
    assert result.stop_reason == STOP_BUDGET
    assert result.reported_total == 9000
    assert result.shortfall == 6500


# --- Phenom ----------------------------------------------------------------

def _phenom_html(offset, total, page_size):
    import json
    rows = [{"title": f"Data Engineer {i}", "cityStateCountry": "Dallas, TX",
             "jobSeqNo": f"REQ{i}", "postedDate": "2026-08-20"}
            for i in range(offset, min(offset + page_size, total))]
    ddo = {"eagerLoadRefineSearch": {"totalHits": total, "data": {"jobs": rows}}}
    return f"<html><script>phApp.ddo = {json.dumps(ddo)};</script></html>"


def _phenom(monkeypatch, total, fail_at=None):
    def fake(url, params=None, **kw):
        offset = params["from"]
        if fail_at is not None and offset >= fail_at:
            raise RuntimeError("HTTP 504")
        return _phenom_html(offset, total, phenom_module.PAGE_SIZE)
    monkeypatch.setattr(phenom_module.http_client, "get_text", fake)
    return PhenomCollector("RTX / Raytheon", {
        "provider": "phenom", "url": "https://careers.rtx.com/global/en",
        "host": "careers.rtx.com", "tenant": "careers"})


def test_phenom_full_walk_is_complete(monkeypatch):
    result = _phenom(monkeypatch, total=35).collect()
    assert result.complete is True
    assert len(result.jobs) == 35


def test_phenom_budget_trip_is_incomplete(monkeypatch):
    """Seven Phenom tenants returned exactly 250 live - the 10x25 ceiling."""
    collector = _phenom(monkeypatch, total=4000)
    monkeypatch.setattr(type(collector), "max_jobs", property(lambda self: 250))
    result = collector.collect()
    assert result.complete is False
    assert result.stop_reason == STOP_BUDGET
    assert len(result.jobs) == 250
    assert result.shortfall == 3750


def test_phenom_failed_page_is_incomplete(monkeypatch):
    result = _phenom(monkeypatch, total=900, fail_at=30).collect()
    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED


# --- Jibe ------------------------------------------------------------------

def _jibe(monkeypatch, total, fail_at=None):
    def fake(url, params=None, **kw):
        page = params["page"]
        if fail_at is not None and page >= fail_at:
            raise RuntimeError("HTTP 502")
        start = (page - 1) * jibe_module.RECORDS_PER_PAGE
        rows = [{"data": {"slug": f"job-{i}", "title": f"Data Engineer {i}",
                          "full_location": "Dallas, TX"}}
                for i in range(start, min(start + jibe_module.RECORDS_PER_PAGE, total))]
        return {"totalCount": total, "jobs": rows}
    monkeypatch.setattr(jibe_module.http_client, "get_json", fake)
    return JibeCollector("Acme", {
        "provider": "jibe", "url": "https://acme.jibeapply.com/",
        "host": "acme.jibeapply.com", "tenant": "acme"})


def test_jibe_full_walk_is_complete(monkeypatch):
    result = _jibe(monkeypatch, total=250).collect()
    assert result.complete is True
    assert len(result.jobs) == 250


def test_jibe_failed_page_is_incomplete(monkeypatch):
    result = _jibe(monkeypatch, total=5000, fail_at=3).collect()
    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED


# --- Cornerstone -----------------------------------------------------------

def _csod(monkeypatch, total, fail_at=None):
    monkeypatch.setattr(
        CornerstoneCollector, "_bootstrap", lambda self, h, c, s: "fake-jwt"
    )

    def fake(url, payload, **kw):
        page = payload["pageNumber"]
        if fail_at is not None and page >= fail_at:
            raise RuntimeError("HTTP 500")
        start = (page - 1) * cornerstone_module.PAGE_SIZE
        rows = [{"requisitionId": i, "displayJobTitle": f"Data Engineer {i}",
                 "locations": [{"city": "Fort Worth", "state": "TX", "country": "US"}]}
                for i in range(start, min(start + cornerstone_module.PAGE_SIZE, total))]
        return {"status": 0, "data": {"totalCount": total, "requisitions": rows}}

    monkeypatch.setattr(cornerstone_module.http_client, "post_json", fake)
    return CornerstoneCollector("JPS Health Network", {
        "provider": "cornerstone",
        "url": "https://jpshealthnet.csod.com/ux/ats/careersite/4/home?c=jpshealthnet",
        "host": "jpshealthnet.csod.com", "tenant": "jpshealthnet"})


def test_cornerstone_full_walk_is_complete(monkeypatch):
    result = _csod(monkeypatch, total=178).collect()
    assert result.complete is True
    assert len(result.jobs) == 178


def test_cornerstone_failed_page_is_incomplete(monkeypatch):
    result = _csod(monkeypatch, total=5000, fail_at=3).collect()
    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED
