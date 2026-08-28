"""A retry writes into the full run's outputs, per company.

A retry re-runs a handful of companies out of the whole workbook. It used to
write ``output/retry_company_jobs.*`` and ``output/retry_last_run.json``, which
kept the full export honest but left two files to reconcile by hand - while the
dashboard, the digest and the workbook all read the unprefixed one. So a fixed
company stayed listed as broken, and its rediscovered jobs sat in a file
nothing else looked at.

Merging is only safe because the rule is the database's own: a company's old
rows are dropped **only** when its fresh harvest is authoritative enough to
delete against (``removal_sync_allowed``). Every other outcome adds or leaves
alone. The tests below are that rule, case by case, because getting it wrong
deletes real postings rather than merely showing stale ones.
"""

import json

import pytest

from ats.base import STOP_BUDGET
from ats.router import METHOD_API, CompanyResult, RoutePlan
from pipeline import (
    COLLAPSE_FLOOR,
    RunSummary,
    merge_job_rows,
    merge_run_reports,
    read_export_rows,
    read_failure_rows,
    write_merged_outputs,
    write_run_report,
)
from settings import Settings


def _plan(company="Acme", provider="workday", method=METHOD_API):
    return RoutePlan(company=company, url=f"https://{company.lower()}.test/jobs",
                     provider=provider, method=method, source="ats_url")


def _job(company="Acme", n=1, **extra):
    job = {
        "job_id": f"{company.lower()}:{n}",
        "company": company,
        "title": f"Data Engineer {n}",
        "job_url": f"https://{company.lower()}.test/jobs/{n}",
        "run_id": "OLD",
    }
    job.update(extra)
    return job


def _result(company="Acme", jobs=None, *, success=True, complete=True, error=None):
    return CompanyResult(
        company, jobs if jobs is not None else [], _plan(company), success,
        complete=complete, stop_reason=None if complete else STOP_BUDGET,
        error_type=error,
    )


@pytest.fixture()
def cfg(tmp_path) -> Settings:
    return Settings(
        {
            "output": {
                "directory": str(tmp_path / "output"),
                "csv": "company_jobs.csv",
                "json": "company_jobs.json",
                "xlsx": "company_jobs.xlsx",
                "failures": "scraper_failures.csv",
            },
        },
        tmp_path / "settings.yaml",
    )


# --- which rows a retried company replaces ---------------------------------

def test_a_clean_complete_retry_replaces_that_companys_rows():
    """The one case where a row disappearing is real news, not a bad scrape."""
    previous = [_job("Acme", 1), _job("Acme", 2), _job("Globex", 9)]
    fresh = [_job("Acme", 3, run_id="NEW")]

    merged = merge_job_rows(previous, fresh, [_result("Acme", fresh)])

    assert [j["job_id"] for j in merged] == ["globex:9", "acme:3"]


def test_a_company_the_retry_never_visited_is_carried_through_untouched():
    previous = [_job("Globex", 9, title="Kept exactly")]

    merged = merge_job_rows(previous, [], [_result("Acme", [_job("Acme", 1)])])

    assert merged[0]["title"] == "Kept exactly"
    assert merged[0]["run_id"] == "OLD"


def test_a_partial_retry_adds_its_rows_rather_than_replacing_them():
    """It never reached the later pages, so what is missing is missing from
    *our* data, not from the employer's site - the same reason the database
    upserts an incomplete company without syncing removals."""
    previous = [_job("Acme", 1), _job("Acme", 2)]
    fresh = [_job("Acme", 3, run_id="NEW")]

    merged = merge_job_rows(
        previous, fresh, [_result("Acme", fresh, complete=False)]
    )

    assert [j["job_id"] for j in merged] == ["acme:1", "acme:2", "acme:3"]


def test_a_failed_retry_leaves_the_previous_rows_alone():
    """A company we could not reach this time tells us nothing about the rows
    it gave us last time. Dropping them would make a retry that went *worse*
    delete real postings."""
    previous = [_job("Acme", 1), _job("Acme", 2)]

    merged = merge_job_rows(
        previous, [], [_result("Acme", [], success=False, error="Timeout")]
    )

    assert [j["job_id"] for j in merged] == ["acme:1", "acme:2"]


def test_an_empty_harvest_is_not_evidence_every_posting_closed():
    """success + zero jobs is `no_jobs`, and the database does not delete on
    it either - both would be reading a bad read as a closure."""
    previous = [_job("Acme", 1)]

    merged = merge_job_rows(previous, [], [_result("Acme", [])])

    assert [j["job_id"] for j in merged] == ["acme:1"]


def test_a_collapsed_retry_adds_rather_than_replaces():
    """Complete by its own account, but a fraction of last run's harvest. The
    collapse guard distrusts it, so the merge must not delete against it."""
    previous_counts = {"Acme": COLLAPSE_FLOOR * 4}
    previous = [_job("Acme", 1), _job("Acme", 2)]
    fresh = [_job("Acme", 3, run_id="NEW")]
    results = [_result("Acme", [_job("Acme", 3)])]

    merged = merge_job_rows(previous, fresh, results, previous_counts)

    assert [j["job_id"] for j in merged] == ["acme:1", "acme:2", "acme:3"]


def test_a_re_scraped_posting_keeps_one_row_and_the_fresh_values():
    """The same job seen twice is one row: the new copy carries this run's
    date, fit score and change status, so it wins in place."""
    previous = [_job("Acme", 1, fit_score=10), _job("Acme", 2)]
    fresh = [_job("Acme", 1, fit_score=90, run_id="NEW")]

    merged = merge_job_rows(
        previous, fresh, [_result("Acme", fresh, complete=False)]
    )

    assert len(merged) == 2
    assert merged[0]["fit_score"] == 90
    assert merged[0]["run_id"] == "NEW"


def test_no_previous_export_merges_to_exactly_this_runs_rows():
    fresh = [_job("Acme", 1)]
    assert merge_job_rows([], fresh, [_result("Acme", fresh)]) == fresh


# --- the files on disk -----------------------------------------------------

def test_the_merge_writes_the_unprefixed_files_the_dashboard_reads(cfg):
    out = cfg.resolve_path("output.directory", "output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "company_jobs.json").write_text(
        json.dumps([_job("Globex", 9), _job("Acme", 1)]), encoding="utf-8"
    )

    fresh = [_job("Acme", 2)]
    paths = write_merged_outputs(
        fresh, [_result("Acme", fresh)], cfg, run_id="NEW"
    )

    assert paths["csv"].name == "company_jobs.csv"
    assert not (out / "retry_company_jobs.csv").exists()
    rows = read_export_rows(paths["json"])
    assert {r["job_id"] for r in rows} == {"globex:9", "acme:2"}


def test_only_the_retried_rows_are_restamped_with_the_new_run_id(cfg):
    """A carried-over row keeps the run that actually produced it - which is
    the entire reason the column exists."""
    out = cfg.resolve_path("output.directory", "output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "company_jobs.json").write_text(
        json.dumps([_job("Globex", 9)]), encoding="utf-8"
    )

    fresh = [_job("Acme", 1)]
    paths = write_merged_outputs(fresh, [_result("Acme", fresh)], cfg, run_id="NEW")

    by_id = {r["job_id"]: r for r in read_export_rows(paths["json"])}
    assert by_id["globex:9"]["run_id"] == "OLD"
    assert by_id["acme:1"]["run_id"] == "NEW"


def test_a_blocked_company_is_not_dropped_from_the_failures_file(cfg):
    """Blocked companies are never retried, so a retry that rewrote
    scraper_failures.csv from its own results alone would quietly report them
    as fixed."""
    out = cfg.resolve_path("output.directory", "output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "scraper_failures.csv").write_text(
        "company,url,ats_provider,error_type,error_message,timestamp\n"
        "Infosys,https://infosys.test,unknown,AccessDenied,challenge,2026-01-01\n",
        encoding="utf-8",
    )

    write_merged_outputs(
        [], [_result("Acme", [], success=False, error="Timeout")], cfg, run_id="NEW"
    )

    companies = {row["company"] for row in read_failure_rows(out / "scraper_failures.csv")}
    assert companies == {"Infosys", "Acme"}


def test_a_company_that_now_succeeds_leaves_the_failures_file(cfg):
    out = cfg.resolve_path("output.directory", "output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "scraper_failures.csv").write_text(
        "company,url,ats_provider,error_type,error_message,timestamp\n"
        "Acme,https://acme.test,workday,Timeout,gave up,2026-01-01\n",
        encoding="utf-8",
    )

    fresh = [_job("Acme", 1)]
    write_merged_outputs(fresh, [_result("Acme", fresh)], cfg, run_id="NEW")

    assert read_failure_rows(out / "scraper_failures.csv") == []


def test_an_unreadable_previous_export_is_not_a_lost_retry(cfg):
    """Half a file is not a reason to throw away a scrape that just ran."""
    out = cfg.resolve_path("output.directory", "output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "company_jobs.json").write_text("{ not json", encoding="utf-8")

    fresh = [_job("Acme", 1)]
    paths = write_merged_outputs(fresh, [_result("Acme", fresh)], cfg, run_id="NEW")

    assert [r["job_id"] for r in read_export_rows(paths["json"])] == ["acme:1"]


# --- the run report --------------------------------------------------------

def _report(**overrides):
    report = {
        "run_id": "FULL",
        "generated_at": "2026-08-27T21:19:56+00:00",
        "companies_attempted": 3,
        "status_counts": {"success": 1, "partial": 1, "blocked": 1},
        "method_counts": {"direct_api": 3},
        "totals": {"jobs_collected": 100, "new_jobs": 7, "removed_jobs": 2},
        "companies": [
            {"company": "Globex", "status": "success", "method": "direct_api", "jobs": 60},
            {"company": "Acme", "status": "partial", "method": "direct_api", "jobs": 40},
            {"company": "Infosys", "status": "blocked", "method": "playwright", "jobs": 0},
        ],
    }
    report.update(overrides)
    return report


def test_a_fixed_company_stops_being_listed_as_needing_attention():
    current = {
        "run_id": "RETRY",
        "companies": [
            {"company": "Acme", "status": "success", "method": "direct_api", "jobs": 95},
        ],
    }

    merged = merge_run_reports(_report(), current)

    by_company = {row["company"]: row for row in merged["companies"]}
    assert by_company["Acme"]["status"] == "success"
    assert by_company["Acme"]["jobs"] == 95
    assert merged["status_counts"] == {"success": 2, "blocked": 1}


def test_the_report_still_describes_the_whole_workbook():
    """A retry of one company must not shrink the report to one company - it
    is the dashboard's only account of the other 182."""
    merged = merge_run_reports(
        _report(), {"run_id": "RETRY", "companies": [
            {"company": "Acme", "status": "success", "method": "direct_api", "jobs": 95},
        ]},
    )

    assert merged["companies_attempted"] == 3
    assert {row["company"] for row in merged["companies"]} == {
        "Globex", "Acme", "Infosys",
    }
    assert merged["totals"]["jobs_collected"] == 60 + 95 + 0


def test_the_merged_report_does_not_claim_to_be_a_full_run():
    """run_id and generated_at answer "which run saw the whole workbook, and
    when did it finish". A 1-company retry did not, so it says so separately."""
    merged = merge_run_reports(
        _report(), {"run_id": "RETRY", "generated_at": "2026-08-28T09:00:00+00:00",
                    "companies": [{"company": "Acme", "status": "success", "jobs": 95}]},
    )

    assert merged["run_id"] == "FULL"
    assert merged["generated_at"] == "2026-08-27T21:19:56+00:00"
    assert merged["last_retry"]["run_id"] == "RETRY"
    assert merged["last_retry"]["finished_at"] == "2026-08-28T09:00:00+00:00"
    assert merged["last_retry"]["companies"] == ["Acme"]


def test_run_scoped_deltas_are_not_added_across_two_baselines():
    """new/changed/removed are measured against different baselines in the two
    runs; summing them would be arithmetic on two different questions."""
    merged = merge_run_reports(
        _report(), {"run_id": "RETRY", "companies": [
            {"company": "Acme", "status": "success", "jobs": 95},
        ]},
    )

    assert merged["totals"]["new_jobs"] == 7
    assert merged["totals"]["removed_jobs"] == 2


def test_a_retried_company_the_full_run_never_listed_is_still_recorded():
    merged = merge_run_reports(
        _report(), {"run_id": "RETRY", "companies": [
            {"company": "Newco", "status": "success", "method": "direct_api", "jobs": 5},
        ]},
    )

    assert merged["companies_attempted"] == 4
    assert merged["companies"][-1]["company"] == "Newco"


def test_write_run_report_merges_in_place_when_asked(tmp_path, cfg):
    out = cfg.resolve_path("output.directory", "output")
    out.mkdir(parents=True, exist_ok=True)
    (out / "last_run.json").write_text(json.dumps(_report()), encoding="utf-8")

    summary = RunSummary()
    path = write_run_report(
        summary, [_result("Acme", [_job("Acme", 1)])], out, "RETRY",
        merge_into_previous=True,
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "last_run.json"
    assert report["run_id"] == "FULL"
    assert report["companies_attempted"] == 3
    assert {row["company"] for row in report["companies"]} == {
        "Globex", "Acme", "Infosys",
    }


def test_merging_with_no_previous_report_writes_this_runs_own(cfg):
    out = cfg.resolve_path("output.directory", "output")

    path = write_run_report(
        RunSummary(), [_result("Acme", [_job("Acme", 1)])], out, "RETRY",
        merge_into_previous=True,
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["run_id"] == "RETRY"
    assert report["companies_attempted"] == 1


# --- what a merged retry still may not do ----------------------------------

def test_a_merged_retry_does_not_speak_for_the_companies_it_skipped():
    """It writes the unprefixed files now, so "the prefix is empty" stopped
    being the same question as "this run saw everything". A retry that sent
    the digest, or wrote Data Retrieved back for 183 companies after visiting
    21, would be reporting on companies it never opened."""
    from pipeline import speaks_for_whole_workbook

    assert speaks_for_whole_workbook("", False) is True
    assert speaks_for_whole_workbook("", True) is False
    assert speaks_for_whole_workbook("test_", False) is False
