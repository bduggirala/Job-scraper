"""Offline tests for the Amazon.jobs collector.

No network: a trimmed two-job ``search.json`` fixture is served for every page
via a monkeypatched fetch, and parsing, pagination stop and the empty-response
failure path are asserted against it.
"""

import pytest

from ats.amazon import AMAZON, RESULT_LIMIT, AmazonJobsCollector
from ats.base import CollectorUnavailable

# Two real-shaped jobs trimmed to the fields the parser reads. ``hits`` is 2 so
# the collector must stop after the first page (offset RESULT_LIMIT >= 2).
SEARCH_PAYLOAD = {
    "error": None,
    "hits": 2,
    "jobs": [
        {
            "title": "Sr. Mechanical Design Engineer",
            "job_path": "/en/jobs/10510154/sr-mechanical-design-engineer",
            "normalized_location": "Herndon, Virginia, USA",
            "city": "Herndon",
            "state": "VA",
            "country_code": "USA",
            "posted_date": "August 21, 2026",
            "job_schedule_type": "full-time",
        },
        {
            "title": "Software Development Engineer",
            "job_path": "/en/jobs/99999999/software-development-engineer",
            "normalized_location": None,
            "city": "Seattle",
            "state": "WA",
            "country_code": "USA",
            "posted_date": "August 20, 2026",
            "job_schedule_type": "full-time",
        },
    ],
}


def _collector():
    detection = {
        "provider": AMAZON,
        "url": "https://www.amazon.jobs/en/",
        "host": "www.amazon.jobs",
        "tenant": "amazon",
    }
    return AmazonJobsCollector("Amazon", detection)


def test_parse_jobs_extracts_title_location_and_absolute_url():
    rows = _collector()._parse_jobs(SEARCH_PAYLOAD["jobs"])
    assert len(rows) == 2

    eng = next(r for r in rows if r["title"] == "Sr. Mechanical Design Engineer")
    assert eng["location"] == "Herndon, Virginia, USA"  # normalized_location preferred
    assert eng["job_url"] == (
        "https://www.amazon.jobs/en/jobs/10510154/sr-mechanical-design-engineer"
    )
    assert eng["employment_type"] == "full-time"
    assert eng["date_posted"] is not None  # "August 21, 2026" parses
    assert eng["ats_provider"] == AMAZON


def test_location_falls_back_to_city_state_country():
    rows = _collector()._parse_jobs(SEARCH_PAYLOAD["jobs"])
    sde = next(r for r in rows if r["title"] == "Software Development Engineer")
    assert sde["location"] == "Seattle, WA, USA"


def test_collect_stops_when_offset_reaches_hits(monkeypatch):
    # Every page returns the same 2-job payload; because hits == 2 and
    # RESULT_LIMIT >= 2, collect() must fetch exactly one page (offset 0).
    calls = {"offsets": []}

    def fake_fetch(self, offset):
        calls["offsets"].append(offset)
        return SEARCH_PAYLOAD

    monkeypatch.setattr(AmazonJobsCollector, "_fetch_page", fake_fetch)
    jobs = _collector().collect()
    assert len(jobs) == 2
    assert calls["offsets"] == [0]  # no second page once offset >= hits


def test_collect_paginates_until_offset_exceeds_hits(monkeypatch):
    # A larger hits count forces a second page; the second page repeats the
    # same URLs so dedupe keeps the row count at 2 while proving pagination ran.
    payload = dict(SEARCH_PAYLOAD, hits=RESULT_LIMIT + 1)
    calls = {"offsets": []}

    def fake_fetch(self, offset):
        calls["offsets"].append(offset)
        return payload

    monkeypatch.setattr(AmazonJobsCollector, "_fetch_page", fake_fetch)
    jobs = _collector().collect()
    assert len(jobs) == 2
    assert calls["offsets"] == [0, RESULT_LIMIT]  # walked one extra page


def test_collect_raises_on_empty_response(monkeypatch):
    empty = {"error": None, "hits": 0, "jobs": []}
    monkeypatch.setattr(AmazonJobsCollector, "_fetch_page", lambda self, offset: empty)
    with pytest.raises(CollectorUnavailable):
        _collector().collect()


def test_collect_raises_when_first_request_fails(monkeypatch):
    def boom(self, offset):
        raise RuntimeError("network down")

    monkeypatch.setattr(AmazonJobsCollector, "_fetch_page", boom)
    with pytest.raises(CollectorUnavailable):
        _collector().collect()
