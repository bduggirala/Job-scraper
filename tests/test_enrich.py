"""Workday detail enrichment.

Workday reports ``"3 Locations"`` for any multi-location requisition, which is
fatal for a metro-area filter: a job open in Dallas, McLean and Chicago would
never match "Dallas". Enrichment fetches the detail record to recover the real
locations and an exact posting date.

The contract that matters most is that it can only ever *add* information - a
failed lookup returns the original row untouched, because dropping a job
because its detail fetch 500'd would be a much worse outcome than keeping a
coarse location.
"""

import pytest

import enrich as enrich_module
from enrich import _workday_detail_url, enrich_records, needs_enrichment


def _job(location="3 Locations", provider="workday",
         url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/Dallas/Data-Engineer_R1"):
    return {
        "title": "Data Engineer", "location": location,
        "ats_provider": provider, "job_url": url,
    }


# --- what needs enriching --------------------------------------------------

@pytest.mark.parametrize("location", ["3 Locations", "2 locations", "12 Locations", None, ""])
def test_aggregate_and_missing_locations_are_enriched(location):
    assert needs_enrichment(_job(location=location)) is True


@pytest.mark.parametrize("location", ["Plano, TX", "Dallas, TX | Austin, TX"])
def test_a_real_location_is_left_alone(location):
    assert needs_enrichment(_job(location=location)) is False


def test_only_workday_is_enriched():
    """No other collector has a detail endpoint wired up."""
    assert needs_enrichment(_job(provider="greenhouse")) is False


# --- detail URL mapping ----------------------------------------------------

def test_a_public_workday_url_maps_to_its_cxs_detail_endpoint():
    detail = _workday_detail_url(
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Dallas/Data-Engineer_R1"
    )
    assert detail == (
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Dallas/Data-Engineer_R1"
    )


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/acme/jobs/123",   # not Workday
    "https://acme.wd5.myworkdayjobs.com/en-US/External",  # no /job/ segment
    "not a url at all",
])
def test_unmappable_urls_return_none(url):
    assert _workday_detail_url(url) is None


# --- enrichment behaviour --------------------------------------------------

def _detail(**kw):
    info = {"location": "Plano, TX", "startDate": "2026-08-20"}
    info.update(kw)
    return {"jobPostingInfo": info}


def test_real_locations_replace_the_aggregate_label(monkeypatch):
    monkeypatch.setattr(
        enrich_module.http_client, "get_json",
        lambda url, **kw: _detail(additionalLocations=["Dallas, TX", "Austin, TX"]),
    )

    enriched = enrich_records([_job()])[0]

    assert "Plano, TX" in enriched["location"]
    assert "Dallas, TX" in enriched["location"]


def test_the_exact_start_date_replaces_a_relative_one(monkeypatch):
    monkeypatch.setattr(enrich_module.http_client, "get_json", lambda url, **kw: _detail())

    enriched = enrich_records([_job()])[0]

    assert enriched["date_posted"] == "2026-08-20"


def test_a_remote_type_sets_the_remote_flag(monkeypatch):
    monkeypatch.setattr(
        enrich_module.http_client, "get_json",
        lambda url, **kw: _detail(remoteType="Fully Remote"),
    )

    assert enrich_records([_job()])[0]["remote"] is True


def test_a_failed_lookup_returns_the_original_row(monkeypatch):
    """Enrichment can only add information - never drop or damage a job."""
    def boom(url, **kw):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(enrich_module.http_client, "get_json", boom)

    original = _job()
    enriched = enrich_records([original])

    assert len(enriched) == 1
    assert enriched[0]["location"] == "3 Locations"


def test_a_malformed_detail_payload_is_survived(monkeypatch):
    monkeypatch.setattr(enrich_module.http_client, "get_json", lambda url, **kw: {"junk": 1})

    assert enrich_records([_job()])[0]["location"] == "3 Locations"


def test_rows_that_need_nothing_are_not_fetched(monkeypatch):
    calls = {"n": 0}

    def counted(url, **kw):
        calls["n"] += 1
        return _detail()

    monkeypatch.setattr(enrich_module.http_client, "get_json", counted)

    enrich_records([_job(location="Plano, TX"), _job(provider="lever")])

    assert calls["n"] == 0, "enrichment fetched a row that did not need it"


def test_ordering_is_preserved(monkeypatch):
    monkeypatch.setattr(enrich_module.http_client, "get_json", lambda url, **kw: _detail())

    rows = [_job(location="Plano, TX"), _job(), _job(location="Austin, TX")]
    enriched = enrich_records(rows)

    assert enriched[0]["location"] == "Plano, TX"
    assert enriched[2]["location"] == "Austin, TX"
