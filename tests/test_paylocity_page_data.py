"""Paylocity ships its whole board inside the page, and we were not reading it.

Texans Credit Union returned **zero** jobs on every route: the JSON endpoint
the collector tries first is gone (the ``/recruiting/v2/api/jobs`` path answers
with the SPA shell, not JSON), the page carries no JSON-LD, its job links are
built by JavaScript so an anchor scrape finds nothing, and Playwright saw an
empty virtualized list. The board was never empty - it was 18 live postings
sitting in a ``window.pageData`` blob in the HTML the first GET already had.

A second defect sat behind the first and could not fire while the first one
held: ``_try_html`` referenced ``STOP_EXHAUSTED`` without importing it, so the
one path that *could* have returned rows would have raised ``NameError`` the
moment it found any. Both are covered here.
"""

from __future__ import annotations

import json

import pytest

import ats.paylocity as paylocity_module
from ats.base import STOP_EXHAUSTED, CollectorUnavailable
from ats.paylocity import PaylocityCollector

GUID = "1934ff17-218d-4324-bec6-ecc4c5ddcfc4"
URL = f"https://recruiting.paylocity.com/recruiting/jobs/All/{GUID}/Texans-Credit-Union"


def _job(job_id, title, city="Richardson", state="TX", **extra):
    job = {
        "JobId": job_id,
        "JobTitle": title,
        "LocationName": "Richardson-Campbell Rd",
        "PublishedDate": "2026-08-26T10:03:37-05:00",
        "Description": "Position Purpose and Objectives {braces} inside",
        "JobLocation": {"LocationId": 1, "City": city, "State": state},
        "IsRemote": False,
    }
    job.update(extra)
    return job


def _page(jobs, prefix="", suffix=""):
    """A page shaped like the real one: the blob inside a <script>."""
    blob = json.dumps({"Departments": ["All Departments"], "Jobs": jobs})
    return (
        f"<html><head>{prefix}</head><body><div class='job-listing-container'></div>"
        f"<script>window.pageData = {blob};</script>{suffix}</body></html>"
    )


def _collect(monkeypatch, html, api_error=RuntimeError("no JSON here")):
    def dead_api(*a, **k):
        raise api_error

    monkeypatch.setattr(paylocity_module.http_client, "get_json", dead_api)
    monkeypatch.setattr(paylocity_module.http_client, "get_text", lambda *a, **k: html)
    collector = PaylocityCollector(
        "Texans Credit Union", {"url": URL, "identifier": GUID}
    )
    return collector.collect()


def test_the_embedded_board_is_read_when_every_other_path_is_empty(monkeypatch):
    result = _collect(monkeypatch, _page([
        _job(4452021, "Fraud Specialist"),
        _job(4450611, "Data Engineer"),
        _job(4446889, "Part-Time Teller - Wylie", city="Wylie"),
    ]))

    assert len(result.jobs) == 3
    assert {j["title"] for j in result.jobs} == {
        "Fraud Specialist", "Data Engineer", "Part-Time Teller - Wylie",
    }


def test_the_html_path_no_longer_raises_nameerror_on_success(monkeypatch):
    """``STOP_EXHAUSTED`` was used but never imported.

    The bug was unreachable only because the path never found rows; the
    moment it did, it crashed instead of returning them.
    """
    result = _collect(monkeypatch, _page([_job(1, "Data Engineer")]))
    assert result.stop_reason == STOP_EXHAUSTED
    assert result.complete is True


def test_a_posting_url_is_built_from_the_job_id(monkeypatch):
    """The row carries no link; the board constructs one, and so must we."""
    result = _collect(monkeypatch, _page([_job(4452021, "Data Engineer")]))
    assert result.jobs[0]["job_url"] == (
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4452021"
    )


def test_the_structured_address_wins_over_the_branch_nickname(monkeypatch):
    """"Richardson-Campbell Rd" names no state, so the DFW matcher cannot use it."""
    result = _collect(monkeypatch, _page([_job(1, "Data Engineer", city="Wylie")]))
    assert result.jobs[0]["location"] == "Wylie, TX"


def test_the_nickname_is_used_when_there_is_no_structured_address(monkeypatch):
    job = _job(1, "Data Engineer")
    del job["JobLocation"]
    result = _collect(monkeypatch, _page([job]))
    assert result.jobs[0]["location"] == "Richardson-Campbell Rd"


def test_a_description_full_of_braces_does_not_truncate_the_blob(monkeypatch):
    r"""Balanced-brace scanning, not a non-greedy regex.

    Descriptions routinely contain ``{`` and ``}``; a lazy ``\{.*?\}`` stops
    inside the first posting and loses every one after it.
    """
    jobs = [_job(i, f"Data Engineer {i}",
                 Description="benefits {a: {b: 1}} and more }{ text")
            for i in range(1, 13)]
    result = _collect(monkeypatch, _page(jobs))
    assert len(result.jobs) == 12


def test_rows_without_a_job_id_are_dropped_not_given_a_broken_link(monkeypatch):
    good = _job(4452021, "Data Engineer")
    bad = _job(4452022, "Ghost Role")
    del bad["JobId"]
    result = _collect(monkeypatch, _page([good, bad]))

    assert len(result.jobs) == 1
    assert result.jobs[0]["title"] == "Data Engineer"


def test_the_posted_date_survives_with_its_offset(monkeypatch):
    """The freshness window is decided on this value."""
    result = _collect(monkeypatch, _page([_job(1, "Data Engineer")]))
    assert "2026-08-26" in str(result.jobs[0]["date_posted"])


def test_an_absent_blob_falls_through_rather_than_crashing(monkeypatch):
    """No pageData, no JSON-LD, no links - still an orderly CollectorUnavailable."""
    with pytest.raises(CollectorUnavailable):
        _collect(monkeypatch, "<html><body>nothing here</body></html>")


def test_a_malformed_blob_falls_through_rather_than_crashing(monkeypatch):
    with pytest.raises(CollectorUnavailable):
        _collect(monkeypatch, "<script>window.pageData = {not valid json;</script>")


def test_an_empty_board_is_not_reported_as_jobs(monkeypatch):
    """A real "we are not hiring" must stay distinguishable from a parse failure."""
    with pytest.raises(CollectorUnavailable):
        _collect(monkeypatch, _page([]))


def test_the_working_api_is_still_preferred(monkeypatch):
    """The blob is a fallback; it must not displace a healthy endpoint."""
    monkeypatch.setattr(
        paylocity_module.http_client, "get_json",
        lambda *a, **k: [{"jobId": 9, "title": "From The API", "location": "Dallas, TX"}],
    )

    def fail(*a, **k):
        raise AssertionError("the HTML path must not run when the API answers")

    monkeypatch.setattr(paylocity_module.http_client, "get_text", fail)
    collector = PaylocityCollector("Texans Credit Union", {"url": URL, "identifier": GUID})

    assert [j["title"] for j in collector.collect().jobs] == ["From The API"]
