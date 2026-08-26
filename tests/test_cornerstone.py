"""Offline tests for the Cornerstone OnDemand (CSOD) collector.

No network: the careersite bootstrap and the search POST are both
monkeypatched. The fixtures are trimmed copies of the real
``jpshealthnet.csod.com`` responses (context token page + search envelope).
"""


import pytest

from ats.base import CollectorUnavailable
from ats.cornerstone import CornerstoneCollector

# Trimmed careersite home page carrying the anonymous JWT the SPA bootstraps
# with (real pages embed this as ``if(!csod.context...) csod.context={...};``).
HOME_HTML = """
<html><head><title>Careers</title></head><body>
<script>if(!csod.context || !csod.context.token) csod.context={"corp":"jpshealthnet",
"user":-103,"cultureID":1,"cultureName":"en-US","endpoints":{"cloud":"https://us.api.csod.com/","api":"/"},
"token":"eyJhbGciOiJIUzUxMiJ9.TESTTOKEN.sig","page":"home"};</script>
</body></html>
"""

# Two-page search response shaped like the real v1/search "data" envelope.
PAGE_1 = {
    "status": 0,
    "data": {
        "totalCount": 3,
        "requisitions": [
            {
                "requisitionId": 30400,
                "postingEffectiveDate": "8/21/2026",
                "displayJobTitle": "Project Coordinator - Patient Experience",
                "locations": [{"city": "Fort Worth", "state": "TX", "country": "US"}],
            },
            {
                "requisitionId": 30394,
                "postingEffectiveDate": "8/21/2026",
                "displayJobTitle": "Multiskilled Tech T5 - Surgery Unit - Nights",
                "locations": [{"city": "Fort Worth", "state": "TX", "country": "US"}],
            },
        ],
    },
}
PAGE_2 = {
    "status": 0,
    "data": {
        "totalCount": 3,
        "requisitions": [
            {
                "requisitionId": 30437,
                "postingEffectiveDate": "8/20/2026",
                "displayJobTitle": "Patient Access Rep PRN - Community Health",
                "locations": [{"city": "Fort Worth", "state": "TX", "country": "US"}],
            }
        ],
    },
}


def _collector():
    detection = {
        "provider": "cornerstone",
        "url": "https://jpshealthnet.csod.com/ux/ats/careersite/4/home?c=jpshealthnet",
        "host": "jpshealthnet.csod.com",
        "tenant": "jpshealthnet",
    }
    return CornerstoneCollector("JPS Health Network", detection)


def _patch_home(monkeypatch, html=HOME_HTML):
    monkeypatch.setattr("ats.cornerstone.http_client.get_text", lambda *a, **k: html)


def _patch_search(monkeypatch, pages):
    calls = {"n": 0}

    def fake_post(url, payload, **kwargs):
        # PAGE_SIZE is large; the fixture drives pagination via pageNumber.
        idx = payload["pageNumber"] - 1
        calls["n"] += 1
        return pages[idx] if idx < len(pages) else {"status": 0, "data": {"totalCount": 3, "requisitions": []}}

    monkeypatch.setattr("ats.cornerstone.http_client.post_json", fake_post)
    return calls


def test_bootstrap_lifts_token(monkeypatch):
    _patch_home(monkeypatch)
    token = _collector()._bootstrap("jpshealthnet.csod.com", "jpshealthnet", 4)
    assert token == "eyJhbGciOiJIUzUxMiJ9.TESTTOKEN.sig"


def test_bootstrap_handles_nested_braces_in_context(monkeypatch):
    # The real context object nests {endpoints:{...}} before the token; a
    # non-greedy brace match would stop early and miss it.
    _patch_home(monkeypatch)
    token = _collector()._bootstrap("jpshealthnet.csod.com", "jpshealthnet", None)
    assert token.endswith(".sig")


def test_site_id_read_from_url():
    assert _collector()._site_id_from_url() == 4


def test_parses_title_location_date_and_absolute_url(monkeypatch):
    _patch_home(monkeypatch)
    # One page holding all 3 so totalCount is satisfied in a single request.
    _patch_search(monkeypatch, [{"status": 0, "data": {
        "totalCount": 3,
        "requisitions": PAGE_1["data"]["requisitions"] + PAGE_2["data"]["requisitions"],
    }}])
    jobs = _collector().collect().jobs
    assert len(jobs) == 3

    row = next(j for j in jobs if j["title"] == "Patient Access Rep PRN - Community Health")
    assert row["location"] == "Fort Worth, TX, US"
    assert row["date_posted"].startswith("2026-08-20")
    assert row["job_url"] == (
        "https://jpshealthnet.csod.com/ux/ats/careersite/4/job/30437?c=jpshealthnet"
    )
    assert row["ats_provider"] == "cornerstone"
    assert row["employment_type"] is None  # absent from the search response


def test_pagination_stops_when_totalcount_reached(monkeypatch):
    # Force a small page size so the 3 fixture jobs span two pages, and assert
    # collect() stops after totalCount is exhausted rather than looping.
    monkeypatch.setattr("ats.cornerstone.PAGE_SIZE", 2)
    _patch_home(monkeypatch)
    calls = _patch_search(monkeypatch, [PAGE_1, PAGE_2])
    jobs = _collector().collect().jobs
    assert len(jobs) == 3
    assert calls["n"] == 2  # page 1 (2 jobs) + page 2 (1 job) -> total reached


def test_raises_when_search_returns_empty(monkeypatch):
    _patch_home(monkeypatch)
    _patch_search(monkeypatch, [{"status": 0, "data": {"totalCount": 0, "requisitions": []}}])
    with pytest.raises(CollectorUnavailable):
        _collector().collect()


def test_raises_when_context_token_missing(monkeypatch):
    _patch_home(monkeypatch, html="<html><body>no context here</body></html>")
    with pytest.raises(CollectorUnavailable):
        _collector().collect()
