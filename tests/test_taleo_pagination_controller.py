"""Taleo's two hand-rolled walks, and the three things they never had.

Ten of the workbook's 180 companies route to ``ats/taleo.py`` - JPMorgan Chase,
Texas Instruments, Honeywell, Oracle, Digital Realty, Baylor Scott & White
Health, Texas Health Resources, Tenet Healthcare, Molina Healthcare and
PlainsCapital Bank - and both of its paths (Oracle Cloud Recruiting and the
legacy career section) walk their pages by hand rather than through
``ats.pagination.paginate``.

That leaves them without the three things centralising the walk bought every
other collector:

* **per-page retry** - one 503 on page 4 of 40 ended the walk and marked the
  company incomplete, which suppresses its removal sync until a clean run.
* **total reconciliation** - Oracle Cloud reports ``TotalJobsCount`` on every
  response and the loop never compared its harvest against it, so a tenant that
  stopped serving rows early was reported as a complete scrape.
* **repeated-page detection** - a tenant ignoring its own ``offset`` serves the
  same page until the job budget runs out.

These are the same defects that were fixed everywhere else; Taleo was simply
never migrated.
"""

import pytest

import ats.taleo as taleo_module
from ats.base import (
    STOP_EXHAUSTED,
    STOP_PAGE_FAILED,
    STOP_SHORT_OF_TOTAL,
    CollectionResult,
    CollectorUnavailable,
)
from ats.taleo import ORC_PAGE_SIZE, TaleoCollector


# --- Oracle Cloud Recruiting ----------------------------------------------

def _orc(monkeypatch, *, total, served=None, fail_at_offset=None, fail_times=99,
         freeze_offset=False):
    """An ORC tenant serving ``total`` requisitions, with injectable faults."""
    served = served or ORC_PAGE_SIZE
    calls: list[int] = []
    failures = {"n": 0}

    def fake(url, params=None, **kw):
        finder = (params or {}).get("finder", "")
        offset = int(finder.split("offset=")[1].split(",")[0])
        calls.append(offset)
        if fail_at_offset is not None and offset == fail_at_offset:
            failures["n"] += 1
            if failures["n"] <= fail_times:
                raise RuntimeError("HTTP 503")
        start = 0 if freeze_offset else offset
        reqs = [
            {"Id": f"REQ{i}", "Title": f"Data Engineer {i}",
             "PrimaryLocation": "Dallas, TX", "PostedDate": "2026-08-20"}
            for i in range(start, min(start + served, total))
        ]
        return {"items": [{"TotalJobsCount": total, "requisitionList": reqs}]}

    monkeypatch.setattr(taleo_module.http_client, "get_json", fake)
    collector = TaleoCollector("Honeywell", {
        "provider": "taleo", "host": "honeywell.fa.oraclecloud.com",
        "tenant": "honeywell", "site": "CX_1",
        "url": "https://honeywell.fa.oraclecloud.com/hcmUI/CandidateExperience"
               "/en/sites/CX_1/requisitions"})
    return collector, calls


def test_orc_retries_a_transient_page_failure_and_still_completes(monkeypatch):
    """One 503 on page two must not cost the company its removal sync."""
    collector, calls = _orc(monkeypatch, total=500,
                            fail_at_offset=ORC_PAGE_SIZE, fail_times=1)

    result = collector.collect()

    assert isinstance(result, CollectionResult)
    assert result.complete is True, "a retryable page truncated the scrape"
    assert len(result.jobs) == 500
    assert calls.count(ORC_PAGE_SIZE) == 2, "the failed page was never retried"


def test_orc_marks_a_page_failing_every_attempt_incomplete(monkeypatch):
    collector, _ = _orc(monkeypatch, total=1000, fail_at_offset=ORC_PAGE_SIZE * 2)

    result = collector.collect()

    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED
    assert len(result.jobs) == ORC_PAGE_SIZE * 2, "the rows we did get are kept"


def test_orc_first_page_failure_still_falls_back_to_the_browser(monkeypatch):
    """A dead first page must raise, so the router can route around it."""
    collector, _ = _orc(monkeypatch, total=500, fail_at_offset=0)

    with pytest.raises(CollectorUnavailable):
        collector.collect()


def test_orc_reconciles_its_harvest_against_the_reported_total(monkeypatch):
    """The tenant says 900 and serves 40; that is not a complete scrape."""
    collector, _ = _orc(monkeypatch, total=40, served=ORC_PAGE_SIZE)
    # Report a total far above what is actually served.
    original = taleo_module.http_client.get_json

    def overreporting(url, params=None, **kw):
        data = original(url, params=params, **kw)
        data["items"][0]["TotalJobsCount"] = 900
        return data

    monkeypatch.setattr(taleo_module.http_client, "get_json", overreporting)

    result = collector.collect()

    assert len(result.jobs) == 40, "the rows we did get are still kept"
    assert result.complete is False, (
        "40 of a reported 900 was reported as a complete scrape"
    )
    assert result.stop_reason == STOP_SHORT_OF_TOTAL
    assert result.reported_total == 900


def test_orc_stops_when_a_tenant_ignores_its_own_offset(monkeypatch):
    """Serving page one forever must not burn the whole job budget."""
    collector, calls = _orc(monkeypatch, total=9000, freeze_offset=True)

    result = collector.collect()

    assert len(calls) < 10, f"kept requesting a repeated page {len(calls)} times"
    assert len(result.jobs) == ORC_PAGE_SIZE


def test_orc_walks_every_page_of_an_ordinary_tenant(monkeypatch):
    collector, calls = _orc(monkeypatch, total=450)

    result = collector.collect()

    assert len(result.jobs) == 450
    assert result.complete is True
    assert calls[:3] == [0, ORC_PAGE_SIZE, ORC_PAGE_SIZE * 2]


# --- Legacy career section -------------------------------------------------

def _legacy(monkeypatch, *, total, served=25, fail_at_page=None, fail_times=99):
    calls: list[int] = []
    failures = {"n": 0}

    def fake(url, payload, params=None, **kw):
        page = payload["pageNo"]
        calls.append(page)
        if fail_at_page is not None and page == fail_at_page:
            failures["n"] += 1
            if failures["n"] <= fail_times:
                raise RuntimeError("HTTP 503")
        start = (page - 1) * served
        rows = [
            {"jobId": i + 1,
             "column": [f"Data Engineer {i}", "Dallas, TX", "2026-08-20"]}
            for i in range(start, min(start + served, total))
        ]
        return {"requisitionList": rows}

    monkeypatch.setattr(taleo_module.http_client, "post_json", fake)
    collector = TaleoCollector("Texas Health Resources", {
        "provider": "taleo", "host": "texashealth.taleo.net",
        "tenant": "texashealth", "site": "ex3",
        "url": "https://texashealth.taleo.net/careersection/ex3/moresearch.ftl"})
    return collector, calls


def test_legacy_retries_a_transient_page_failure_and_still_completes(monkeypatch):
    collector, calls = _legacy(monkeypatch, total=200, fail_at_page=3, fail_times=1)

    result = collector.collect()

    assert result.complete is True, "a retryable page truncated the scrape"
    assert len(result.jobs) == 200
    assert calls.count(3) == 2, "the failed page was never retried"


def test_legacy_marks_a_page_failing_every_attempt_incomplete(monkeypatch):
    collector, _ = _legacy(monkeypatch, total=500, fail_at_page=3)

    result = collector.collect()

    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED
    assert len(result.jobs) == 50


def test_legacy_still_walks_a_portal_serving_fifteen_a_page(monkeypatch):
    """The behaviour the hand-rolled loop got right; it must survive the move."""
    collector, _ = _legacy(monkeypatch, total=95, served=15)

    result = collector.collect()

    assert len(result.jobs) == 95
    assert result.complete is True
    assert result.stop_reason == STOP_EXHAUSTED
