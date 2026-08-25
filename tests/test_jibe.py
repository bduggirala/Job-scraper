"""Offline tests for the Jibe (iCIMS-owned) collector.

No network: the collector's ``_fetch_page`` is monkeypatched to serve a trimmed
fixture shaped like the real ``/api/jobs`` response. Covers parsing, the
page-count stop, the no-new-slugs stop, and CollectorUnavailable on empty.
"""

import pytest

from ats.base import CollectorUnavailable
from ats.jibe import JibeCollector

# Two real-shaped Concentra jobs, trimmed to the fields the parser reads, wrapped
# in the ``{"jobs": [{"data": {...}}], "totalCount": N}`` envelope the API emits.
PAGE_1 = {
    "totalCount": 2,
    "count": 2,
    "jobs": [
        {
            "data": {
                "slug": "351474",
                "req_id": "351474",
                "title": "Physician Assistant Float or Nurse Practitioner Float",
                "full_location": "Columbus, Ohio",
                "city": "Columbus",
                "state": "Ohio",
                "country": "United States",
                "posted_date": "2026-08-20T21:34:00+0000",
                "employment_type": "FULL_TIME",
                "description": "Overview<br><br>Join Concentra.",
                "apply_url": "https://careers-concentrainc.icims.com/jobs/351474/login",
            }
        },
        {
            "data": {
                "slug": "351999",
                "req_id": "351999",
                "title": "Medical Assistant",
                "city": "Dallas",
                "state": "Texas",
                "country": "United States",
                "posted_date": "2026-08-19T10:00:00+0000",
                "employment_type": "PART_TIME",
                "apply_url": "https://careers-concentrainc.icims.com/jobs/351999/login",
            }
        },
    ],
}


def _collector():
    detection = {
        "provider": "jibe",
        "url": "https://concentrahealthservices.jibeapply.com",
        "host": "concentrahealthservices.jibeapply.com",
        "tenant": "concentrahealthservices",
    }
    return JibeCollector("Concentra", detection)


def test_parses_title_location_url_and_apply(monkeypatch):
    monkeypatch.setattr(
        JibeCollector, "_fetch_page",
        lambda self, endpoint, page: PAGE_1 if page == 1 else {"jobs": [], "totalCount": 2},
    )
    rows = _collector().collect().jobs
    assert len(rows) == 2

    pa = next(r for r in rows if r["title"].startswith("Physician Assistant"))
    assert pa["location"] == "Columbus, Ohio"          # full_location preferred
    assert pa["job_url"] == "https://concentrahealthservices.jibeapply.com/jobs/351474"
    assert pa["apply_url"] == "https://careers-concentrainc.icims.com/jobs/351474/login"
    assert pa["employment_type"] == "FULL_TIME"
    assert pa["date_posted"] == "2026-08-20T21:34:00+00:00"
    assert pa["ats_provider"] == "jibe"

    ma = next(r for r in rows if r["title"] == "Medical Assistant")
    assert ma["location"] == "Dallas, Texas, United States"  # joined from discrete parts


def test_stops_at_total_count_without_extra_pages(monkeypatch):
    # totalCount == 2 and page 1 supplies both, so collect() must not fetch page 2.
    calls = {"n": 0}

    def fake(self, endpoint, page):
        calls["n"] += 1
        return PAGE_1

    monkeypatch.setattr(JibeCollector, "_fetch_page", fake)
    rows = _collector().collect().jobs
    assert len(rows) == 2
    assert calls["n"] == 1  # stopped once len(seen) >= totalCount


def test_stops_when_page_has_no_new_slugs(monkeypatch):
    # No totalCount: page 1 has jobs, page 2 repeats them (no new slugs) -> stop.
    page = dict(PAGE_1)
    page.pop("totalCount")
    page.pop("count")
    calls = {"n": 0}

    def fake(self, endpoint, page_num):
        calls["n"] += 1
        return page

    monkeypatch.setattr(JibeCollector, "_fetch_page", fake)
    rows = _collector().collect().jobs
    assert len(rows) == 2
    assert calls["n"] == 2  # page 1 (2 new) + page 2 (0 new -> stop)


def test_raises_collector_unavailable_on_empty(monkeypatch):
    monkeypatch.setattr(
        JibeCollector, "_fetch_page", lambda self, endpoint, page: {"jobs": [], "totalCount": 0}
    )
    with pytest.raises(CollectorUnavailable):
        _collector().collect()
