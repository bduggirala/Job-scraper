"""Tests for the CollectionResult completeness contract.

A collector that fetched 3 of 25 pages and one that fetched all 25 previously
returned the same type - a bare list - so no caller could tell them apart. The
pipeline then deleted every stored job absent from the partial harvest. These
tests pin the contract that makes partial collection visible.
"""

import pytest

from ats.base import CollectionResult


def test_a_result_is_complete_by_default():
    """A collector that says nothing about completeness claims a full walk."""
    result = CollectionResult(jobs=[{"title": "Data Engineer"}])
    assert result.complete is True
    assert result.stop_reason is None


def test_an_incomplete_result_carries_its_reason():
    result = CollectionResult(
        jobs=[{"title": "Data Engineer"}],
        complete=False,
        pages_fetched=3,
        reported_total=500,
        stop_reason="page_failed",
    )
    assert result.complete is False
    assert result.pages_fetched == 3
    assert result.reported_total == 500
    assert result.stop_reason == "page_failed"


def test_a_bare_list_from_an_unconverted_collector_is_treated_as_complete():
    """The migration shim: collectors not yet converted must keep working."""
    result = CollectionResult.coerce([{"title": "Data Engineer"}])

    assert isinstance(result, CollectionResult)
    assert result.complete is True
    assert result.jobs == [{"title": "Data Engineer"}]


def test_coerce_passes_a_real_result_through_untouched():
    original = CollectionResult(jobs=[], complete=False, stop_reason="budget_exhausted")
    assert CollectionResult.coerce(original) is original


def test_coerce_handles_none_as_an_empty_complete_result():
    result = CollectionResult.coerce(None)
    assert result.jobs == []
    assert result.complete is True


def test_shortfall_is_reported_when_fewer_jobs_than_the_api_claimed():
    result = CollectionResult(
        jobs=[{"n": i} for i in range(500)], reported_total=8000
    )
    assert result.shortfall == 7500


def test_shortfall_is_zero_when_the_total_was_never_reported():
    result = CollectionResult(jobs=[{"n": 1}], reported_total=None)
    assert result.shortfall == 0


def test_shortfall_is_never_negative_when_a_tenant_under_reports_its_total():
    """Some tenants report a stale total lower than the rows they serve."""
    result = CollectionResult(jobs=[{"n": i} for i in range(10)], reported_total=4)
    assert result.shortfall == 0
