"""What the Run Scraper tab reports, and where each number comes from.

The rule these tests exist to hold: a run is never called successful because a
process was launched. The exit code decides success or failure, and only then
does ``output/last_run.json`` refine "completed" into "partial".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from dashboard import services
from settings import Settings


@pytest.fixture()
def cfg(tmp_path) -> Settings:
    settings = Settings(
        {
            "output": {
                "directory": str(tmp_path / "output"),
                "csv": "company_jobs.csv",
                "json": "company_jobs.json",
                "xlsx": "company_jobs.xlsx",
                "failures": "scraper_failures.csv",
            },
            "logging": {"file": str(tmp_path / "logs" / "scraper.log")},
            "hours_old": 168,
        },
        tmp_path / "settings.yaml",
    )
    (tmp_path / "output").mkdir()
    (tmp_path / "logs").mkdir()
    return settings


def write_report(cfg, **overrides) -> dict:
    report = {
        "run_id": "20260827T202307Z",
        "generated_at": "2026-08-27T21:19:56.652032+00:00",
        "companies_attempted": 183,
        "status_counts": {"success": 152, "partial": 18, "no_jobs": 7, "failed": 4, "blocked": 2},
        "method_counts": {"direct_api": 123, "playwright": 60},
        "totals": {
            "jobs_collected": 143580, "matching_jobs": 104, "new_jobs": 7,
            "changed_jobs": 1, "removed_jobs": 2154, "duplicates_removed": 3,
        },
        "companies": [
            {"company": "Boingo Wireless", "status": "success", "provider": "greenhouse",
             "method": "direct_api", "jobs": 6, "removal_sync_allowed": True},
            {"company": "Ericsson", "status": "failed", "provider": "eightfold",
             "method": "playwright", "jobs": 0, "error_type": "Timeout",
             "error_message": "Exceeded the 900s per-company limit",
             "removal_sync_allowed": False},
            {"company": "Infosys", "status": "blocked", "provider": "unknown",
             "method": "playwright", "jobs": 0, "error_type": "AccessDenied",
             "error_message": "Site answered with a bot challenge",
             "removal_sync_allowed": False},
            {"company": "IBM", "status": "partial", "provider": "workday",
             "method": "playwright", "jobs": 280, "reported_total": 1016,
             "stop_reason": "budget_exhausted", "removal_sync_allowed": False},
        ],
    }
    report.update(overrides)
    services.last_run_report_path(cfg).write_text(
        json.dumps(report), encoding="utf-8"
    )
    return report


def write_launch(cfg, **overrides) -> None:
    state = {
        "started_at": "2026-08-27T20:23:07+00:00",
        "finished_at": "2026-08-27T21:19:56+00:00",
        "exit_code": 0,
        "args": ["--no-email"],
        "dry_run": False,
        "error": "",
        "console_tail": "",
    }
    state.update(overrides)
    services.run_state_path(cfg).write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def test_the_run_id_is_the_runs_own_start_time():
    moment = services.parse_run_id("20260827T202307Z")
    assert moment == datetime(2026, 8, 27, 20, 23, 7, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["", None, "not-a-run-id", "2026-08-27", "20261301T000000Z"])
def test_a_bad_run_id_is_none_rather_than_an_exception(value):
    assert services.parse_run_id(value) is None


def test_timestamps_come_back_as_aware_utc():
    assert services.parse_timestamp("2026-08-27T21:19:56.652032+00:00").tzinfo is not None
    assert services.parse_timestamp("2026-08-27T21:19:56Z").hour == 21
    # A naive stamp is read as UTC, matching how the pipeline writes them.
    assert services.parse_timestamp("2026-08-27T21:19:56").tzinfo == timezone.utc
    assert services.parse_timestamp("garbage") is None
    assert services.parse_timestamp(None) is None


def test_the_clock_is_12_hour_with_a_readable_date():
    """The ISO form ran together into a digit string; this is the legible one."""
    moment = datetime(2026, 8, 27, 20, 23, 7, tzinfo=timezone.utc)
    shown = services.format_clock(moment)

    assert "Aug" in shown
    assert shown.endswith(("AM", "PM")) or " AM " in shown or " PM " in shown
    assert ":" in shown
    # No leading zero on the hour, and the year is spelled out.
    assert " 0" not in shown.split(" - ")[-1]
    assert "2026" in shown


def test_the_clock_drops_seconds_on_request():
    moment = datetime(2026, 8, 27, 20, 5, 7, tzinfo=timezone.utc)
    with_seconds = services.format_clock(moment)
    without = services.format_clock(moment, seconds=False)
    assert with_seconds.count(":") == 2
    assert without.count(":") == 1


def test_the_clock_is_a_dash_when_there_is_no_time():
    assert services.format_clock(None) == "-"


def test_utc_stays_available_alongside_the_local_clock():
    """UTC remains the authoritative value; the clock is only the display."""
    moment = datetime(2026, 8, 27, 20, 23, 7, tzinfo=timezone.utc)
    assert services.format_utc(moment) == "2026-08-27 20:23:07 UTC"


def test_freshness_reads_as_prose():
    now = datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc)
    twelve_minutes_ago = now - timedelta(minutes=12)
    assert services.humanize_age(twelve_minutes_ago, now=now) == "12 minutes ago"
    assert services.humanize_age(now - timedelta(seconds=1), now=now) == "1 second ago"
    assert services.humanize_age(now - timedelta(hours=2, minutes=4), now=now) == "2h 04m ago"
    assert services.humanize_age(None) == ""


# ---------------------------------------------------------------------------
# Missing and malformed inputs
# ---------------------------------------------------------------------------

def test_no_files_at_all_is_idle_not_an_error(cfg):
    status = services.run_status(cfg)
    assert status.status == services.STATUS_IDLE
    assert status.report_available is False
    assert status.report_malformed is False
    assert "No run has been recorded" in status.detail


def test_a_malformed_run_report_is_reported_as_such(cfg):
    services.last_run_report_path(cfg).write_text("{ half a file", encoding="utf-8")

    assert services.load_last_run(cfg) is None
    status = services.run_status(cfg)
    assert status.report_malformed is True
    assert status.status == services.STATUS_IDLE
    assert "unreadable" in status.detail


def test_a_json_report_missing_its_companies_block_is_rejected(cfg):
    services.last_run_report_path(cfg).write_text('{"run_id": "x"}', encoding="utf-8")
    assert services.load_last_run(cfg) is None


def test_an_empty_run_report_file_is_not_a_crash(cfg):
    services.last_run_report_path(cfg).write_text("", encoding="utf-8")
    assert services.load_last_run(cfg) is None
    assert services.run_status(cfg).report_malformed is True


# ---------------------------------------------------------------------------
# Exit codes decide the verdict
# ---------------------------------------------------------------------------

def test_a_nonzero_exit_code_is_a_failure_whatever_the_report_says(cfg):
    write_report(cfg, status_counts={"success": 183})
    write_launch(cfg, exit_code=2)

    status = services.run_status(cfg)
    assert status.status == services.STATUS_FAILED
    assert status.exit_code == 2
    assert "exited with code 2" in status.detail


def test_a_clean_exit_with_troubled_companies_is_partial(cfg):
    write_report(cfg)
    write_launch(cfg, exit_code=0)

    status = services.run_status(cfg)
    assert status.status == services.STATUS_PARTIAL
    assert status.successful_companies == 152
    assert status.partial_companies == 18
    assert status.failed_companies == 4
    assert status.blocked_companies == 2
    assert status.companies_attempted == 183


def test_a_clean_exit_with_nothing_troubled_is_completed(cfg):
    write_report(cfg, status_counts={"success": 180, "no_jobs": 3})
    write_launch(cfg, exit_code=0)
    assert services.run_status(cfg).status == services.STATUS_COMPLETED


def test_a_report_where_nothing_was_reached_is_failed(cfg):
    write_report(cfg, status_counts={"failed": 12, "blocked": 3})
    write_launch(cfg, exit_code=0)
    assert services.run_status(cfg).status == services.STATUS_FAILED


def test_a_dry_run_does_not_borrow_the_previous_runs_verdict(cfg):
    write_report(cfg)
    write_launch(cfg, exit_code=0, dry_run=True)

    status = services.run_status(cfg)
    assert status.status == services.STATUS_COMPLETED
    assert "no jobs scraped" in status.detail


def test_a_live_lock_wins_over_every_file(cfg, monkeypatch):
    write_report(cfg)
    write_launch(cfg, exit_code=0)
    monkeypatch.setattr(services, "pid_alive", lambda pid: True)
    services.run_lock_path(cfg).write_text(
        json.dumps({"pid": 999, "started_at": services.utcnow().isoformat()}),
        encoding="utf-8",
    )

    status = services.run_status(cfg)
    assert status.status == services.STATUS_RUNNING
    assert status.running is True
    assert status.duration_seconds is not None


def test_an_interrupted_run_is_reported_as_failed_with_a_recovery_hint(cfg, monkeypatch):
    monkeypatch.setattr(services, "pid_alive", lambda pid: False)
    services.run_lock_path(cfg).write_text(
        json.dumps({"pid": 999, "started_at": services.utcnow().isoformat()}),
        encoding="utf-8",
    )

    status = services.run_status(cfg)
    assert status.status == services.STATUS_FAILED
    assert status.stale_lock is True
    assert "Clear the stale lock" in status.detail


def test_a_report_with_no_dashboard_launch_still_dates_itself(cfg):
    """A run started from a terminal is readable too - via its run id."""
    write_report(cfg)

    status = services.run_status(cfg)
    assert status.started_at == datetime(2026, 8, 27, 20, 23, 7, tzinfo=timezone.utc)
    assert status.finished_at == services.parse_timestamp("2026-08-27T21:19:56.652032+00:00")
    assert int(status.duration_seconds) == 3409  # 20:23:07 -> 21:19:56
    assert status.status == services.STATUS_PARTIAL


# ---------------------------------------------------------------------------
# The current output
# ---------------------------------------------------------------------------

CSV_HEADER = (
    "company,title,location,date_posted,job_url,apply_url,ats_provider,"
    "scraping_method,date_filter_status,change_status\n"
)


def write_jobs_csv(cfg, rows: str) -> None:
    services.output_files(cfg)["csv"].write_text(CSV_HEADER + rows, encoding="utf-8")


def test_no_export_yet_is_an_empty_frame_not_a_crash(cfg):
    assert services.load_current_jobs(cfg).empty


def test_the_export_is_read_as_written(cfg):
    write_jobs_csv(cfg, (
        "AT&T,Lead Data Engineer,\"Dallas, Texas\",2026-08-27T20:26:01+00:00,"
        "https://example.com/a,,workday,direct_api,within_window,new\n"
        "IBM,Data Platform Engineer,\"Plano, TX\",2026-08-20T10:00:00+00:00,"
        "https://example.com/b,,workday,playwright,older_than_window,unchanged\n"
    ))

    jobs = services.load_current_jobs(cfg)
    assert len(jobs) == 2
    assert list(jobs["company"]) == ["AT&T", "IBM"]
    assert services.within_window_count(jobs) == 1
    assert services.change_status_counts(jobs) == {"new": 1, "unchanged": 1}


def test_a_truncated_export_does_not_take_the_page_down(cfg):
    services.output_files(cfg)["csv"].write_text('a,b\n"unterminated', encoding="utf-8")
    # Either a best-effort parse or an empty frame - never an exception.
    services.load_current_jobs(cfg)


def test_change_counts_are_empty_when_the_column_is_absent():
    assert services.change_status_counts(pd.DataFrame({"company": ["x"]})) == {}
    assert services.within_window_count(pd.DataFrame()) == 0


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

@pytest.fixture()
def jobs() -> pd.DataFrame:
    return pd.DataFrame([
        {"company": "AT&T", "title": "Lead Data Engineer", "location": "Dallas, Texas",
         "date_posted": "2026-08-27T20:26:01+00:00", "change_status": "new"},
        {"company": "IBM", "title": "Data Platform Engineer", "location": "Plano, TX",
         "date_posted": "2026-08-20T10:00:00+00:00", "change_status": "unchanged"},
        {"company": "IBM", "title": "ETL Developer", "location": "Remote",
         "date_posted": "", "change_status": "changed"},
    ])


def test_filters_narrow_by_company_title_location_and_status(jobs):
    assert list(services.filter_jobs(jobs, companies=["IBM"])["title"]) == [
        "Data Platform Engineer", "ETL Developer"
    ]
    assert len(services.filter_jobs(jobs, title="data")) == 2
    assert len(services.filter_jobs(jobs, location="tx")) == 1
    assert len(services.filter_jobs(jobs, statuses=["new", "changed"])) == 2


def test_a_date_filter_keeps_undated_rows(jobs):
    """The pipeline keeps and flags undated jobs; the dashboard must not undo that."""
    filtered = services.filter_jobs(jobs, posted_from="2026-08-25", posted_to="2026-08-28")
    assert set(filtered["title"]) == {"Lead Data Engineer", "ETL Developer"}


def test_filtering_an_empty_frame_is_an_empty_frame():
    assert services.filter_jobs(pd.DataFrame()).empty


# ---------------------------------------------------------------------------
# Companies needing attention
# ---------------------------------------------------------------------------

def test_only_troubled_companies_are_listed_with_their_reason(cfg):
    report = write_report(cfg)
    frame = services.problem_companies(report)

    assert list(frame["Company"]) == ["IBM", "Ericsson", "Infosys"]
    assert list(frame["Status"]) == ["partial", "failed", "blocked"]
    assert frame.loc[frame["Company"] == "Ericsson", "Detail"].iloc[0].startswith("Exceeded")
    assert frame.loc[frame["Company"] == "IBM", "Stop reason"].iloc[0] == "budget_exhausted"


def test_no_report_means_no_table():
    assert services.problem_companies(None).empty
    assert services.problem_companies({"companies": []}).empty


# ---------------------------------------------------------------------------
# Progress, read out of the log the run already writes
# ---------------------------------------------------------------------------

LOG = """\
2026-08-27 16:23:14,099 [DEBUG] ats.resolver: Southwest Airlines: resolved -> phenom
2026-08-27 16:23:21,174 [INFO] pipeline: Executing 183 companies: 144 via direct API, 39 via Playwright
2026-08-27 16:23:21,174 [INFO] ats.router: Judge Group -> Icims
2026-08-27 16:23:21,175 [INFO] ats.router: Boingo Wireless -> Greenhouse
2026-08-27 16:23:22,001 [INFO] ats.router: Judge Group -> 15 jobs retrieved
2026-08-27 16:23:22,500 [INFO] ats.router: Capital One -> Workday
"""


def test_progress_counts_started_and_finished_companies():
    progress = services.parse_progress(LOG)
    assert progress.total == 183
    assert progress.started == 3
    assert progress.finished == 1
    assert progress.current_company == "Capital One"
    assert progress.provider == "Workday"
    assert 0 < progress.fraction < 1


def test_a_completion_line_is_not_mistaken_for_a_provider():
    """`ats.router` logs each company twice - routed, then retrieved."""
    progress = services.parse_progress(
        "[INFO] pipeline: Executing 1 companies: 1 via direct API, 0 via Playwright\n"
        "[INFO] ats.router: CHRISTUS Health -> Playwright fallback\n"
        "[INFO] ats.router: CHRISTUS Health -> 1 jobs retrieved\n"
    )
    assert progress.started == 1  # counted once, not twice
    assert progress.finished == 1
    assert progress.provider == "Playwright fallback"
    assert progress.fraction == 1.0


def test_progress_ignores_router_lines_logged_before_execution_began():
    progress = services.parse_progress(
        "[INFO] ats.router: Stale Co -> Workday\n"
        "[INFO] ats.router: Stale Co -> 3 jobs retrieved\n"
        "[INFO] pipeline: Executing 2 companies: 2 via direct API, 0 via Playwright\n"
        "[INFO] ats.router: Real Co -> Lever\n"
    )
    assert progress.started == 1
    assert progress.finished == 0
    assert progress.current_company == "Real Co"


def test_progress_before_the_totals_line_has_no_fraction():
    progress = services.parse_progress("[INFO] pipeline: Routing 183 companies...\n")
    assert progress.total is None
    assert progress.fraction is None


def test_progress_survives_a_missing_log(cfg):
    assert services.run_progress(cfg).total is None


def test_a_log_tail_is_bounded_and_missing_logs_are_blank(cfg):
    log = services.log_path(cfg)
    log.write_text("".join(f"line {i}\n" for i in range(500)), encoding="utf-8")
    assert services.read_log_tail(log, 10).splitlines() == [f"line {i}" for i in range(490, 500)]
    assert services.read_log_tail(cfg.resolve_path("logging.file").with_name("absent.log")) == ""
