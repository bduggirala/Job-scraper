"""ats.discovery must survive collectors returning a CollectionResult.

``verify_ats_url`` drives a candidate URL through its real collector and counts
what comes back. It previously did ``len(jobs or [])`` on the return value,
which breaks outright once a collector returns a CollectionResult instead of a
list - and that path is how ``tools/find_ats_urls.py`` verifies every candidate,
so the whole discovery tool would fail on any converted provider.
"""

import pytest

import ats.discovery as discovery
from ats.base import STOP_PAGE_FAILED, ATSCollector, CollectionResult


class _ResultCollector(ATSCollector):
    provider = "workday"

    def collect(self) -> CollectionResult:
        return CollectionResult(jobs=[{"title": "Data Engineer"}] * 12)


class _IncompleteCollector(ATSCollector):
    provider = "workday"

    def collect(self) -> CollectionResult:
        return CollectionResult(
            jobs=[{"title": "Data Engineer"}] * 500,
            complete=False, reported_total=8200, stop_reason=STOP_PAGE_FAILED,
        )


class _LegacyListCollector(ATSCollector):
    provider = "greenhouse"

    def collect(self):
        return [{"title": "Data Engineer"}] * 7


def test_a_converted_collector_is_counted_not_crashed_on(monkeypatch):
    monkeypatch.setitem(discovery.COLLECTORS, "workday", _ResultCollector)

    count, note = discovery.verify_ats_url(
        "Capital One", "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/"
    )

    assert count == 12
    assert "12 jobs" in note


def test_an_unconverted_collector_still_verifies(monkeypatch):
    monkeypatch.setitem(discovery.COLLECTORS, "greenhouse", _LegacyListCollector)

    count, note = discovery.verify_ats_url(
        "Acme", "https://boards.greenhouse.io/acme"
    )

    assert count == 7


def test_an_incomplete_harvest_still_verifies_but_says_so(monkeypatch):
    """A truncated walk still proves the URL works - it just isn't the whole list."""
    monkeypatch.setitem(discovery.COLLECTORS, "workday", _IncompleteCollector)

    count, note = discovery.verify_ats_url(
        "Capital One", "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/"
    )

    assert count == 500
    assert "INCOMPLETE" in note
    assert STOP_PAGE_FAILED in note
