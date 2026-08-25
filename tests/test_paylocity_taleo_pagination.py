"""Pagination for the two collectors that never really had it.

* Paylocity requested ``pageNumber: 1`` and stopped - the clearest instance of
  a hard-coded first-page request in the codebase.
* Legacy Taleo inferred end-of-results from ``len(requisitions) < 25`` without
  ever sending a page size, so a portal serving 15 rows a page stopped after
  page one and reported success.
"""

import pytest

import ats.paylocity as paylocity_module
import ats.taleo as taleo_module
from ats.base import STOP_PAGE_FAILED, CollectionResult
from ats.paylocity import PaylocityCollector
from ats.taleo import TaleoCollector

GUID = "1934ff17-218d-4324-bec6-e00000000000"


# --- Paylocity -------------------------------------------------------------

def _paylocity(monkeypatch, total, page_size=200, fail_at_page=None):
    calls: list[dict] = []

    def fake(url, params=None, **kw):
        calls.append(dict(params or {}))
        page = params["pageNumber"]
        if fail_at_page is not None and page >= fail_at_page:
            raise RuntimeError("HTTP 503")
        start = (page - 1) * page_size
        # Ids start at 1: Paylocity reads `jobId or id`, so a zero id would
        # be treated as absent and the row dropped without a URL.
        return [
            {"jobId": i + 1, "title": f"Data Engineer {i}", "location": "Dallas, TX"}
            for i in range(start, min(start + page_size, total))
        ]

    monkeypatch.setattr(paylocity_module.http_client, "get_json", fake)
    collector = PaylocityCollector("Texans Credit Union", {
        "provider": "paylocity", "identifier": GUID, "tenant": GUID,
        "url": f"https://recruiting.paylocity.com/recruiting/jobs/All/{GUID}"})
    return collector, calls


def test_paylocity_walks_past_the_first_page(monkeypatch):
    collector, calls = _paylocity(monkeypatch, total=460)

    result = collector.collect()

    assert isinstance(result, CollectionResult)
    assert len(result.jobs) == 460
    assert [c["pageNumber"] for c in calls][:3] == [1, 2, 3]


def test_paylocity_stops_on_a_short_page(monkeypatch):
    collector, calls = _paylocity(monkeypatch, total=150)

    result = collector.collect()

    assert len(result.jobs) == 150
    assert len(calls) == 1, "a short first page means there is no page 2"
    assert result.complete is True


def test_paylocity_marks_a_failed_later_page_incomplete(monkeypatch):
    collector, _ = _paylocity(monkeypatch, total=900, fail_at_page=3)

    result = collector.collect()

    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED
    assert len(result.jobs) == 400


# --- Legacy Taleo ----------------------------------------------------------

def _taleo(monkeypatch, total, page_size):
    calls: list[dict] = []

    def fake(url, payload, params=None, **kw):
        calls.append(payload)
        page = payload["pageNo"]
        start = (page - 1) * page_size
        rows = [
            {"jobId": i + 1, "column": [f"Data Engineer {i}", "Dallas, TX", "2026-08-20"]}
            for i in range(start, min(start + page_size, total))
        ]
        return {"requisitionList": rows}

    monkeypatch.setattr(taleo_module.http_client, "post_json", fake)
    collector = TaleoCollector("Texas Health Resources", {
        "provider": "taleo", "host": "texashealth.taleo.net",
        "tenant": "texashealth", "site": "ex3",
        "url": "https://texashealth.taleo.net/careersection/ex3/moresearch.ftl"})
    return collector, calls


def test_taleo_does_not_stop_early_on_a_portal_serving_fifteen_a_page(monkeypatch):
    """The defect: 25 was hard-coded, so a 15-row portal ended after page 1."""
    collector, calls = _taleo(monkeypatch, total=95, page_size=15)

    result = collector.collect()

    assert len(result.jobs) == 95, "stopped early on a non-25 page size"
    assert len(calls) >= 6


def test_taleo_sends_an_explicit_page_size(monkeypatch):
    collector, calls = _taleo(monkeypatch, total=40, page_size=25)

    collector.collect()

    assert calls, "no request made"
    assert any("pageSize" in str(c) or "PAGE_SIZE" in str(c).upper() for c in calls), (
        f"no explicit page size sent: {calls[0]}"
    )


def test_taleo_confirms_the_end_rather_than_assuming_it(monkeypatch):
    """A first page shorter than requested is ambiguous.

    It means either "that is every job" or "this portal caps its page size
    below what we asked for" - and those need opposite handling. Assuming the
    first is exactly the bug being fixed here (a 15-row portal read as a
    20-job employer), so the collector spends one extra request to confirm
    instead. It must not spend more than that.
    """
    collector, calls = _taleo(monkeypatch, total=20, page_size=25)

    result = collector.collect()

    assert len(result.jobs) == 20
    assert len(calls) == 2, "should confirm with one page, not keep walking"
    assert result.complete is True
