"""Offline tests for Workday pagination completeness and ordering.

Workday is the single most common provider in the workbook (56 of 180
companies) and the one where truncation was measured live: eleven separate
tenants returned exactly 500 jobs, which is PAGE_SIZE(20) x max_pages(25) - the
ceiling, not the real total.

Two behaviours are pinned here:

* a walk that stops short must say so (``complete=False`` + a stop reason), so
  ``pipeline.run()`` will not treat the missing rows as closed postings;
* the walk must request newest-first, so that when it *is* truncated the rows
  kept are the ones the freshness window can still match.
"""

import pytest

import ats.workday as workday_module
from ats.base import (
    STOP_BUDGET,
    STOP_PAGE_FAILED,
    STOP_TOTAL_REACHED,
    CollectionResult,
)
from ats.workday import PAGE_SIZE, WorkdayCollector


def _posting(index: int) -> dict:
    return {
        "title": f"Data Engineer {index}",
        "locationsText": "Dallas, TX",
        "postedOn": "Posted 2 Days Ago",
        "externalPath": f"/job/Dallas/Data-Engineer_R{100000 + index}",
    }


def _collector(company: str = "Capital One") -> WorkdayCollector:
    return WorkdayCollector(company, {
        "provider": "workday",
        "url": "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/",
        "host": "capitalone.wd12.myworkdayjobs.com",
        "tenant": "capitalone",
        "site": "Capital_One",
    })


class _FakeCXS:
    """Serves paged jobPostings, optionally failing at a chosen page."""

    def __init__(self, total: int, fail_at_offset: int | None = None):
        self.total = total
        self.fail_at_offset = fail_at_offset
        self.payloads: list[dict] = []

    def __call__(self, endpoint, payload, **kwargs):
        self.payloads.append(payload)
        offset = payload["offset"]
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            raise RuntimeError("HTTP 503 from Workday CXS")
        rows = [
            _posting(i) for i in range(offset, min(offset + PAGE_SIZE, self.total))
        ]
        return {"total": self.total, "jobPostings": rows}


def _install(monkeypatch, fake: _FakeCXS) -> None:
    monkeypatch.setattr(workday_module.http_client, "post_json", fake)


def test_a_full_walk_is_marked_complete(monkeypatch):
    _install(monkeypatch, _FakeCXS(total=45))

    result = _collector().collect()

    assert isinstance(result, CollectionResult)
    assert result.complete is True
    assert len(result.jobs) == 45
    assert result.reported_total == 45
    assert result.stop_reason == STOP_TOTAL_REACHED
    assert result.shortfall == 0


def test_a_failed_page_marks_the_walk_incomplete_but_keeps_earlier_rows(monkeypatch):
    """The exact defect: page 3 of a 10-page walk 503s.

    Before this change the collector logged a warning, returned 40 rows, and
    the router reported success - after which sync_company() deleted the
    other 160 jobs from the database.
    """
    _install(monkeypatch, _FakeCXS(total=200, fail_at_offset=40))

    result = _collector().collect()

    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED
    assert len(result.jobs) == 40          # the rows fetched before the failure
    assert result.reported_total == 200
    assert result.shortfall == 160


def test_hitting_the_job_budget_before_the_total_marks_the_walk_incomplete(monkeypatch):
    """A tenant larger than the collector's budget must not look complete."""
    collector = _collector()
    monkeypatch.setattr(type(collector), "max_jobs", property(lambda self: 100))
    _install(monkeypatch, _FakeCXS(total=8000))

    result = collector.collect()

    assert result.complete is False
    assert result.stop_reason == STOP_BUDGET
    assert len(result.jobs) == 100
    assert result.reported_total == 8000
    assert result.shortfall == 7900


def test_the_walk_requests_newest_first(monkeypatch):
    """Truncation is only tolerable if what survives is the freshest rows.

    The collector previously posted searchText:"" with no ordering at all, so
    its 500-job ceiling kept an arbitrary 500 of the tenant's requisitions.
    """
    fake = _FakeCXS(total=45)
    _install(monkeypatch, fake)

    _collector().collect()

    assert fake.payloads, "collector made no request"
    for payload in fake.payloads:
        assert payload.get("searchText") == ""
        assert "POSTING_DATES" in str(payload).upper() or payload.get("sortBy"), (
            f"no newest-first ordering requested: {payload}"
        )


def test_a_tenant_that_runs_out_early_is_complete_despite_a_higher_total(monkeypatch):
    """An empty page means the provider is done, whatever its total claimed.

    Some tenants report a stale ``total`` well above the rows they actually
    serve. Running out is exhaustion, not truncation - marking it incomplete
    would permanently block removal sync for that company.
    """
    class _OverReporting(_FakeCXS):
        def __call__(self, endpoint, payload, **kwargs):
            self.payloads.append(payload)
            offset = payload["offset"]
            rows = [_posting(i) for i in range(offset, min(offset + PAGE_SIZE, 30))]
            return {"total": 900, "jobPostings": rows}

    _install(monkeypatch, _OverReporting(total=30))

    result = _collector().collect()

    assert result.complete is True
    assert len(result.jobs) == 30
