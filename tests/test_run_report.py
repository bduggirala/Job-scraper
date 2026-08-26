"""What a finished run leaves behind, beyond the list of jobs.

Two things were missing, and they are the same thing at two scales.

**Per company**, a run's own knowledge of what happened - which provider was
used, how it was extracted, how long it took, whether it finished, and crucially
whether removal detection was allowed to run - existed only as log lines and a
counter. ``output/scraper_failures.csv`` covers failures; nothing covered the
successes, so "did Honeywell actually finish this time?" could only be answered
by grepping a log.

**Per run**, the spreadsheet identified itself by filename alone. A
``company_jobs.xlsx`` sitting in someone's downloads folder could have been
generated yesterday or last month, and nothing in it said which.

The four statuses are deliberately distinct, because they call for different
responses: ``failed`` needs investigation, ``blocked`` needs a different route
in (never a workaround), ``partial`` means the data is real but incomplete, and
``no_jobs`` means the site was read correctly and had nothing.
"""

import json

import pytest

from ats.base import STOP_BUDGET, STOP_PAGE_FAILED
from ats.router import METHOD_API, METHOD_BROWSER, CompanyResult, RoutePlan
from pipeline import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_NO_JOBS,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    RunSummary,
    company_status,
    write_run_report,
)


def _plan(company="Acme", provider="workday", method=METHOD_API):
    return RoutePlan(company=company, url=f"https://{company.lower()}.test/jobs",
                     provider=provider, method=method, source="ats_url")


def _job(n=1):
    return {"job_id": f"acme:{n}", "title": "Data Engineer",
            "company": "Acme", "job_url": f"https://acme.test/jobs/{n}"}


# --- status classification -------------------------------------------------

def test_a_complete_scrape_with_jobs_is_a_success():
    result = CompanyResult("Acme", [_job()], _plan(), True)
    assert company_status(result) == STATUS_SUCCESS


def test_a_truncated_scrape_is_partial_not_success():
    """The data is real; the coverage is not. Conflating them is what hid
    eleven Workday tenants all returning exactly 500 jobs for months."""
    result = CompanyResult("Acme", [_job()], _plan(), True,
                           complete=False, stop_reason=STOP_BUDGET)
    assert company_status(result) == STATUS_PARTIAL


def test_a_bot_challenge_is_blocked_not_failed():
    """Blocked needs a different route in, never a workaround; failed needs a
    fix. Reporting them together sends both down the wrong path."""
    result = CompanyResult("Acme", [], _plan(), False, error_type="AccessDenied")
    assert company_status(result) == STATUS_BLOCKED


def test_an_error_is_a_failure():
    result = CompanyResult("Acme", [], _plan(), False, error_type="Timeout")
    assert company_status(result) == STATUS_FAILED


def test_reaching_a_site_that_had_nothing_is_its_own_status():
    """Not a failure: the site was read correctly and is not hiring."""
    result = CompanyResult("Acme", [], _plan(), True)
    assert company_status(result) == STATUS_NO_JOBS


# --- the run report --------------------------------------------------------

def test_the_report_records_every_company_attempted(tmp_path):
    results = [
        CompanyResult("Acme", [_job()], _plan("Acme"), True),
        CompanyResult("Beta", [], _plan("Beta"), False, error_type="Timeout"),
    ]
    path = write_run_report(RunSummary(companies_scanned=2), results, tmp_path, "R1")

    report = json.loads(path.read_text(encoding="utf-8"))
    assert [c["company"] for c in report["companies"]] == ["Acme", "Beta"]


def test_each_company_row_carries_what_a_diagnosis_needs(tmp_path):
    result = CompanyResult(
        "Acme", [_job(), _job(2)], _plan(provider="taleo"), True,
        complete=False, stop_reason=STOP_PAGE_FAILED, reported_total=900,
        error_type=None, duration_seconds=12.5,
    )
    path = write_run_report(RunSummary(), [result], tmp_path, "R1")

    row = json.loads(path.read_text(encoding="utf-8"))["companies"][0]
    assert row["provider"] == "taleo"
    assert row["method"] == METHOD_API
    assert row["jobs"] == 2
    assert row["reported_total"] == 900
    assert row["stop_reason"] == STOP_PAGE_FAILED
    assert row["duration_seconds"] == pytest.approx(12.5)
    assert row["status"] == STATUS_PARTIAL
    assert row["url"] == "https://acme.test/jobs"


def test_the_report_says_whether_removal_detection_was_allowed(tmp_path):
    """The single most consequential per-company fact, and it was invisible.

    Removal sync runs only for a company that succeeded, returned jobs, AND
    finished - so a reader has no way to tell "these removals are real" from
    "removals were skipped for this company" without it.
    """
    complete = CompanyResult("Acme", [_job()], _plan("Acme"), True)
    truncated = CompanyResult("Beta", [_job()], _plan("Beta"), True,
                              complete=False, stop_reason=STOP_BUDGET)
    empty = CompanyResult("Gamma", [], _plan("Gamma"), True)

    path = write_run_report(RunSummary(), [complete, truncated, empty], tmp_path, "R1")
    rows = {c["company"]: c for c in json.loads(path.read_text(encoding="utf-8"))["companies"]}

    assert rows["Acme"]["removal_sync_allowed"] is True
    assert rows["Beta"]["removal_sync_allowed"] is False, (
        "a truncated company's absent jobs would have been read as closed"
    )
    assert rows["Gamma"]["removal_sync_allowed"] is False


def test_the_report_carries_the_run_id_and_a_timestamp(tmp_path):
    path = write_run_report(RunSummary(), [], tmp_path, "20260826T101500Z")

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["run_id"] == "20260826T101500Z"
    assert report["generated_at"], "nothing dates the run"


def test_the_report_totals_the_four_statuses(tmp_path):
    results = [
        CompanyResult("A", [_job()], _plan("A"), True),
        CompanyResult("B", [_job()], _plan("B"), True, complete=False,
                      stop_reason=STOP_BUDGET),
        CompanyResult("C", [], _plan("C"), False, error_type="AccessDenied"),
        CompanyResult("D", [], _plan("D"), False, error_type="Timeout"),
        CompanyResult("E", [], _plan("E"), True),
    ]
    path = write_run_report(RunSummary(companies_scanned=5), results, tmp_path, "R1")

    counts = json.loads(path.read_text(encoding="utf-8"))["status_counts"]
    assert counts == {STATUS_SUCCESS: 1, STATUS_PARTIAL: 1, STATUS_BLOCKED: 1,
                      STATUS_FAILED: 1, STATUS_NO_JOBS: 1}


def test_the_report_separates_api_extraction_from_the_browser_fallback(tmp_path):
    results = [
        CompanyResult("A", [_job()], _plan("A", method=METHOD_API), True),
        CompanyResult("B", [_job()], _plan("B", method=METHOD_BROWSER), True),
        CompanyResult("C", [_job()], _plan("C", method=METHOD_BROWSER), True),
    ]
    path = write_run_report(RunSummary(), results, tmp_path, "R1")

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["method_counts"] == {METHOD_API: 1, METHOD_BROWSER: 2}


def test_a_partial_run_writes_its_report_under_the_same_prefix(tmp_path):
    """So a --limit run can never overwrite a full run's report."""
    write_run_report(RunSummary(), [], tmp_path, "R1", prefix="test_")
    assert (tmp_path / "test_last_run.json").exists()
    assert not (tmp_path / "last_run.json").exists()


# --- the run id reaches the spreadsheet ------------------------------------

def test_every_exported_row_names_the_run_that_produced_it(tmp_path, monkeypatch):
    """A company_jobs.xlsx on someone's desktop should say when it was made."""
    from settings import load_settings
    from pipeline import OUTPUT_FIELDS, write_outputs

    assert "run_id" in OUTPUT_FIELDS

    cfg = load_settings()
    monkeypatch.setattr(cfg, "resolve_path", lambda key, default=None: tmp_path)

    job = {f: None for f in OUTPUT_FIELDS}
    job.update({"company": "Acme", "title": "Data Engineer",
                "job_url": "https://acme.test/jobs/1"})
    paths = write_outputs([job], [], cfg, run_id="20260826T101500Z")

    import pandas as pd
    frame = pd.read_csv(paths["csv"])
    assert frame.iloc[0]["run_id"] == "20260826T101500Z"
