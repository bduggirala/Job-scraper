"""Deduplication within one run.

Two passes, in order: normalized URL first (the same posting reached by two
routes), then the composite key (the same posting re-listed under a different
URL, but only when company, title and location all agree too). Collapsing on
title+company+location alone would merge genuinely distinct openings.
"""

import pytest

from deduplicate import (
    deduplicate,
    normalize_company,
    normalize_location,
    normalize_title,
)


def _job(url="https://x.test/job/1", title="Data Engineer", company="Acme",
         location="Plano, TX", **kw):
    row = {
        "job_url": url, "title": title, "company": company, "location": location,
        "date_posted": None, "description": None, "scraping_method": "playwright",
    }
    row.update(kw)
    return row


# --- normalization ---------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Acme Inc", "Acme, Inc."),
    ("Acme Corporation", "Acme Corp"),
    ("The Acme Group", "Acme"),
])
def test_company_suffixes_do_not_make_two_employers(a, b):
    assert normalize_company(a) == normalize_company(b)


def test_distinct_companies_stay_distinct():
    assert normalize_company("Acme Inc") != normalize_company("Contoso Inc")


@pytest.mark.parametrize("a,b", [
    ("Dallas, TX, US", "Dallas, TX, United States"),
    ("Dallas, TX, USA", "Dallas TX"),
])
def test_country_suffixes_do_not_make_two_locations(a, b):
    assert normalize_location(a) == normalize_location(b)


def test_title_normalization_ignores_punctuation_and_case():
    assert normalize_title("Sr. Data Engineer") == normalize_title("sr data engineer")


# --- URL pass --------------------------------------------------------------

def test_the_same_posting_reached_twice_is_collapsed():
    result = deduplicate([_job(), _job()])
    assert len(result["jobs"]) == 1
    assert result["removed"] == 1


def test_tracking_parameters_do_not_defeat_url_matching():
    result = deduplicate([
        _job(url="https://x.test/job/1"),
        _job(url="https://x.test/job/1?utm_source=indeed"),
    ])
    assert len(result["jobs"]) == 1


def test_genuinely_different_postings_both_survive():
    result = deduplicate([_job(url="https://x.test/job/1"),
                          _job(url="https://x.test/job/2", title="Data Analyst")])
    assert len(result["jobs"]) == 2
    assert result["removed"] == 0


# --- composite pass --------------------------------------------------------

def test_one_requisition_under_two_urls_is_collapsed_by_job_id():
    """The real cross-route duplicate: an ATS API link and a careers-page link
    to the same requisition. The URLs differ; the requisition id does not."""
    result = deduplicate([
        _job(url="https://x.test/job/R246063", job_id="acme:workday:R246063"),
        _job(url="https://x.test/apply?jobId=R246063", job_id="acme:workday:R246063"),
    ])
    assert len(result["jobs"]) == 1


def test_two_urls_without_a_shared_id_are_left_alone():
    """Without a stable id there is no evidence these are one posting, and
    merging on company+title+location alone would lose real openings."""
    result = deduplicate([
        _job(url="https://x.test/job/1"),
        _job(url="https://x.test/posting/1"),
    ])
    assert len(result["jobs"]) == 2


def test_two_openings_with_one_title_in_different_cities_both_survive():
    """The composite key must not merge distinct openings."""
    result = deduplicate([
        _job(url="https://x.test/job/1", location="Plano, TX"),
        _job(url="https://x.test/job/2", location="Austin, TX"),
    ])
    assert len(result["jobs"]) == 2


# --- which copy survives ---------------------------------------------------

def test_the_copy_carrying_a_real_date_wins():
    poor = _job(date_posted=None)
    rich = _job(date_posted="2026-08-20T00:00:00+00:00")

    kept = deduplicate([poor, rich])["jobs"][0]
    assert kept["date_posted"] is not None


def test_the_api_copy_beats_the_scraped_copy():
    scraped = _job(scraping_method="playwright")
    api = _job(scraping_method="direct_api")

    kept = deduplicate([scraped, api])["jobs"][0]
    assert kept["scraping_method"] == "direct_api"


def test_input_order_does_not_change_which_copy_wins():
    poor = _job(description=None)
    rich = _job(description="Full description")

    assert deduplicate([poor, rich])["jobs"][0]["description"] == "Full description"
    assert deduplicate([rich, poor])["jobs"][0]["description"] == "Full description"


def test_a_row_with_no_url_still_dedupes_on_the_composite_key():
    result = deduplicate([_job(url=None), _job(url=None)])
    assert len(result["jobs"]) == 1


def test_an_empty_run_is_handled():
    result = deduplicate([])
    assert result == {"jobs": [], "removed": 0}
