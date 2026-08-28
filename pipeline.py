"""Run orchestration: Excel in, filtered jobs out.

Execution is deliberately two-phase:

    Phase 1 - route every company (cheap; one optional GET for branded pages)
    Phase 2 - execute, with API companies and browser companies in *separate*
              thread pools so 10 concurrent HTTP workers never translate into
              10 concurrent Chromium instances.

Nothing in this module reads, writes, or reconciles JobSpy output.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pandas as pd

from ats.base import DESCRIBABLE_STOP_REASONS
from ats.router import (
    METHOD_API,
    METHOD_BROWSER,
    SOURCE_LIVE_PAGE,
    CompanyResult,
    RoutePlan,
    fetch_company_jobs,
    plan_route,
)
from database import JobDatabase
from deduplicate import deduplicate
from enrich import enrich_records
from export_ats_urls import write_discovered_urls, write_repaired_urls, write_run_status
from filters import apply_filters
from fit import score_fit
from job_identity import extract_stable_job_id
from logger import get_logger
from normalize import RECORD_FIELDS
from settings import Settings, load_settings

log = get_logger("pipeline")

OUTPUT_FIELDS = list(RECORD_FIELDS) + [
    "date_filter_status",
    # Which date the freshness verdict rests on: the employer's posting date,
    # our own first sighting, or nothing at all. "within_window" conflated the
    # first two, and they are different claims.
    "date_source",
    "location_match_type",
    "remote_scope",
    "source_query",
    "fit_score",
    "fit_matched",
    "fit_explanation",
    "first_seen",
    "is_new",
    # new / changed / unchanged. Change detection reached the database and the
    # email digest but never the spreadsheet, so the file most people actually
    # open could not say which rows had moved since the last run.
    "change_status",
    # Which run produced this file. A company_jobs.xlsx that has been copied
    # somewhere else identified itself by filename alone, so a reader had no
    # way to tell yesterday's export from last month's.
    "run_id",
]

#: Per-company outcomes. Deliberately five rather than success/failure, because
#: they call for different responses: ``failed`` needs a fix, ``blocked`` needs
#: a different route in (never a workaround), ``partial`` means the rows are
#: real but the coverage is not, and ``no_jobs`` means the site was read
#: correctly and simply is not hiring.
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_NO_JOBS = "no_jobs"


def company_status(result: CompanyResult) -> str:
    """Classify one company's outcome."""
    if not result.success:
        return STATUS_BLOCKED if result.error_type == "AccessDenied" else STATUS_FAILED
    if not result.jobs:
        return STATUS_NO_JOBS
    return STATUS_SUCCESS if result.complete else STATUS_PARTIAL


def new_run_id(moment: datetime | None = None) -> str:
    """A sortable identifier for one run, e.g. ``20260826T101500Z``."""
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def removal_sync_allowed(
    result: CompanyResult, previous_counts: dict[str, int] | None = None
) -> bool:
    """Is this company's harvest authoritative enough to delete against?

    The four conditions :func:`sync_completed_companies` applies, as one
    predicate: success AND jobs AND complete AND not collapsed. It was stated
    twice - once as control flow there, once inline in the run report - and a
    third caller (the retry merge, which must drop a company's old rows only
    when this is true) is one copy too many.
    """
    return bool(
        result.success and result.jobs and result.complete
        and not collapsed_against(
            (previous_counts or {}).get(result.company), len(result.jobs)
        )
    )


def write_run_report(
    summary: "RunSummary",
    results: list[CompanyResult],
    out_dir: Path,
    run_id: str,
    prefix: str = "",
    previous_counts: dict[str, int] | None = None,
    merge_into_previous: bool = False,
) -> Path:
    """Write ``last_run.json``: what happened to every company, and why.

    ``merge_into_previous`` splices this run's per-company rows into the report
    already at that path instead of replacing it - what a retry needs, so the
    file keeps describing the whole workbook while telling the truth about the
    companies that were just re-run. See :func:`merge_run_reports`.

    ``scraper_failures.csv`` covers failures only, so the successes - which
    provider answered, how it was extracted, how long it took, whether it
    finished - existed solely as log lines. The field that most needed
    surfacing is ``removal_sync_allowed``: it is the difference between "this
    company's removals are real" and "removals were skipped here", and nothing
    reported it.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    method_counts: dict[str, int] = {}

    for result in results:
        status = company_status(result)
        status_counts[status] = status_counts.get(status, 0) + 1
        method = result.plan.method
        method_counts[method] = method_counts.get(method, 0) + 1
        rows.append({
            "company": result.company,
            "url": result.plan.url,
            "provider": result.plan.provider,
            "method": method,
            "source_column": result.plan.source,
            "resolved_via_page": bool(result.plan.resolved_via_page),
            "fell_back_to_browser": bool(result.fell_back),
            "jobs": len(result.jobs),
            "reported_total": result.reported_total,
            "complete": bool(result.complete),
            "stop_reason": result.stop_reason,
            "status": status,
            "error_type": result.error_type,
            "error_message": (result.error_message or "")[:300] or None,
            "duration_seconds": (
                round(result.duration_seconds, 2)
                if result.duration_seconds is not None else None
            ),
            # Mirrors sync_completed_companies exactly: success AND jobs AND
            # complete AND not collapsed - the one predicate, never restated.
            "removal_sync_allowed": removal_sync_allowed(result, previous_counts),
            "collapsed_vs_previous": collapsed_against(
                (previous_counts or {}).get(result.company), len(result.jobs)
            ),
            "previous_jobs": (previous_counts or {}).get(result.company),
            "discovered_ats_url": result.discovered_ats_url,
            "discovered_provider": result.discovered_provider,
        })

    report = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "companies_attempted": len(results),
        "status_counts": status_counts,
        "method_counts": method_counts,
        "totals": {
            "jobs_collected": summary.jobs_collected,
            "matching_jobs": summary.location_matches,
            "new_jobs": summary.new_jobs,
            "changed_jobs": summary.changed_jobs,
            "removed_jobs": summary.jobs_removed,
            "duplicates_removed": summary.duplicates_removed,
            "incomplete_companies": summary.incomplete_companies,
            "collapsed_companies": summary.collapsed_companies,
        },
        "companies": rows,
    }

    path = out_dir / f"{prefix}last_run.json"
    if merge_into_previous:
        previous = _read_report(path)
        if previous:
            report = merge_run_reports(previous, report)
        else:
            log.info("No report at %s to merge into - writing this run's own", path)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    return path


# ---------------------------------------------------------------------------
# Merging a retry back into the full run's outputs
#
# A retry re-runs a handful of companies out of the whole workbook, so on its
# own it can only ever write a slice. Writing that slice to its own
# ``retry_*`` files kept the full export honest but left two files to read and
# reconcile by hand - and the dashboard, the digest and the workbook all point
# at the unprefixed one. Merging puts the retry's answer where everything
# already looks, per company, without the retry pretending to know anything
# about the companies it never visited.
# ---------------------------------------------------------------------------

def merge_job_rows(
    previous_jobs: list[dict[str, Any]],
    fresh_jobs: list[dict[str, Any]],
    results: list[CompanyResult],
    previous_counts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Splice a partial run's job rows into a full run's export, per company.

    The rule per retried company is the database's own, so the export and
    ``data/jobs.db`` cannot end up disagreeing about the same employer:

    * **Authoritative** (:func:`removal_sync_allowed`) - the scrape succeeded,
      finished, and did not collapse. Its rows *replace* that company's, which
      is the only case where a row disappearing is real news rather than a
      company we failed to read properly.
    * **Succeeded but not authoritative** - partial again, or a suspicious
      drop. Its rows are *added* to that company's, keyed on ``job_id``, with
      the fresh copy winning. This mirrors "upsert, skip the removal sync":
      what it found is real, what it did not reach is not a closure.
    * **Failed** - nothing is touched. A company we could not reach this time
      tells us nothing about the rows it gave us last time.

    Companies the retry never visited are carried through untouched.
    """
    fresh_by_company: dict[str, list[dict[str, Any]]] = {}
    for job in fresh_jobs:
        fresh_by_company.setdefault(job.get("company"), []).append(job)

    replaced = {
        result.company for result in results
        if removal_sync_allowed(result, previous_counts)
    }
    # An empty harvest is not evidence every posting closed, so a company that
    # came back with nothing is "attempted", never "authoritative" - the same
    # reason sync_completed_companies skips it.
    attempted = {result.company for result in results}

    merged: list[dict[str, Any]] = []
    at_id: dict[str, int] = {}
    for job in previous_jobs:
        if job.get("company") in replaced:
            continue
        if job.get("job_id"):
            at_id[job["job_id"]] = len(merged)
        merged.append(job)

    for company in attempted:
        for job in fresh_by_company.get(company, []):
            job_id = job.get("job_id")
            index = at_id.get(job_id) if job_id else None
            if index is not None:
                # Same posting, freshly scraped: the new row carries this
                # run's date, fit score and change status, so it wins.
                merged[index] = job
                continue
            if job_id:
                at_id[job_id] = len(merged)
            merged.append(job)

    return merged


def merge_run_reports(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Splice a retry's per-company rows into the previous full run's report.

    ``run_id`` and ``generated_at`` stay the *full* run's: they answer "which
    run produced this picture of the whole workbook, and when did it finish",
    and a retry of 21 companies did not. The retry identifies itself under
    ``last_retry`` instead, so the file never claims to be something it is not.

    Counts that describe the whole workbook (``status_counts``,
    ``companies_attempted``, ``totals.jobs_collected``) are recomputed from the
    merged rows. Counts that are a *delta* for one run - new, changed, removed,
    duplicates - are left as the full run wrote them: a retry's deltas are
    measured against a different baseline and adding them would be arithmetic
    on two different questions.
    """
    fresh_rows = {
        str(row.get("company")): row for row in (current.get("companies") or [])
    }
    rows: list[dict[str, Any]] = [
        fresh_rows.pop(str(row.get("company")), row)
        for row in (previous.get("companies") or [])
    ]
    # A retried company the previous report never listed (the workbook changed
    # between runs) is appended rather than dropped on the floor.
    rows.extend(fresh_rows.values())

    status_counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status")
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1

    method_counts: dict[str, int] = {}
    for row in rows:
        method = row.get("method")
        if method:
            method_counts[method] = method_counts.get(method, 0) + 1

    merged = dict(previous)
    merged["companies"] = rows
    merged["companies_attempted"] = len(rows)
    merged["status_counts"] = status_counts
    merged["method_counts"] = method_counts

    totals = dict(previous.get("totals") or {})
    totals["jobs_collected"] = sum(int(row.get("jobs") or 0) for row in rows)
    merged["totals"] = totals

    merged["last_retry"] = {
        "run_id": current.get("run_id"),
        "finished_at": current.get("generated_at"),
        "companies": [str(row.get("company")) for row in (current.get("companies") or [])],
    }
    return merged


#: Statuses worth a second attempt. ``blocked`` is deliberately absent: the
#: site issued a challenge or an explicit denial, and asking again is not a fix.
RETRYABLE_STATUSES = (STATUS_FAILED, STATUS_PARTIAL)


def retryable_from_report(report: dict | None) -> list[str]:
    """The retryable company names inside an already-loaded run report.

    Split out from :func:`retryable_companies` so a caller holding the report
    - the dashboard, which has read it to draw its tables - asks the same
    question of the same data instead of keeping a second copy of the rule.
    """
    names: list[str] = []
    for row in (report or {}).get("companies") or []:
        name = str(row.get("company") or "").strip()
        if name and row.get("status") in RETRYABLE_STATUSES and name not in names:
            names.append(name)
    return names


def retryable_companies(report_path: Path) -> list[str]:
    """Companies from a previous run's ``last_run.json`` worth re-running.

    Raises rather than returning an empty list when the report is missing or
    unreadable - "nothing failed" and "there is no report" are opposite facts
    and must not look alike.
    """
    report_path = Path(report_path)
    if not report_path.exists():
        raise FileNotFoundError(
            f"No run report at {report_path}. Run the scraper once first."
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{report_path} is not readable JSON: {exc}") from exc

    return retryable_from_report(report)


#: A harvest this far below the previous run's is treated as a collection
#: failure rather than as an employer closing that many postings at once.
#:
#: ``complete`` catches a walk that *knows* it stopped short. It cannot catch a
#: walk that never found the list: the browser traversal renders a careers site,
#: lands on a "featured roles" panel instead of the job list, and returns four
#: rows believing it saw everything there was. Measured live - Caterpillar
#: collected 138 jobs one run and 4 the next, both reported complete, which put
#: 143 stored postings one miss from deletion.
#:
#: 0.5 rather than something tighter because real boards do move: a seasonal
#: close-out or a hiring freeze can legitimately halve one. Halving is the point
#: at which "the employer did this" stops being the likelier explanation.
COLLAPSE_RATIO = 0.5

#: Below this many previously-collected jobs the ratio is not meaningful - going
#: from 6 postings to 2 is an ordinary week at a small employer, not a cliff.
COLLAPSE_FLOOR = 20

CHANGE_NEW = "new"
CHANGE_CHANGED = "changed"
CHANGE_UNCHANGED = "unchanged"


def previous_costs(report_path: Path) -> dict[str, tuple[bool, float]]:
    """``{company: (timed_out, seconds)}`` from a previous run's report.

    Used only to decide *order*, never to skip a company - a site that hung
    once may be fine today, and this pipeline exists to not miss jobs.

    Empty when there is no readable report; a first run simply keeps workbook
    order.
    """
    report_path = Path(report_path)
    if not report_path.exists():
        return {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.debug("Could not read %s for run ordering: %s", report_path, exc)
        return {}

    costs: dict[str, tuple[bool, float]] = {}
    for row in report.get("companies") or []:
        name = str(row.get("company") or "").strip()
        if not name:
            continue
        # Only a company that burned its *own* per-company limit is a problem
        # company. The other Timeout in the report is "Exceeded the browser
        # phase budget", which is what a healthy company gets when it was
        # queued behind one - counting that would demote the victims and let
        # the real offender keep its place.
        timed_out = (
            str(row.get("error_type") or "") == "Timeout"
            and "per-company limit" in str(row.get("error_message") or "")
        )
        try:
            seconds = float(row.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        costs[name] = (timed_out, seconds)
    return costs


def slowest_last(plans: list[RoutePlan], costs: dict[str, tuple[bool, float]]) -> list[RoutePlan]:
    """Order a phase so its known problem companies run at the end.

    A company that wedges its worker holds it until the process exits -
    Playwright's sync API is thread-affine, so no other thread can close its
    browser, and giving the pool spare threads to "replace" the slot only
    starts a second browser beside the first (tried, measured, reverted: six
    concurrent instances against a ceiling of five turned a 43-minute run with
    3 failures into 3h13m with 19).

    Ordering is the lever that costs nothing. Whatever a wedged company blocks,
    it now blocks companies that were *already* the slowest or already timed
    out - and if the phase budget expires, it expires on them rather than on
    healthy employers that simply queued behind them. Omnicell and Slalom have
    timed out on every run recorded; under this they can no longer take
    anything with them.

    Deliberately not a skip list: a company is only ever deprioritised, so a
    site that recovers is still scraped.
    """
    def sort_key(plan: RoutePlan) -> tuple[int, float]:
        timed_out, seconds = costs.get(plan.company, (False, 0.0))
        return (1 if timed_out else 0, seconds)

    return sorted(plans, key=sort_key)


def _read_report(report_path: Path) -> dict[str, Any] | None:
    """A run report off disk, or ``None`` when there is not a usable one.

    Never raises: every caller here is deciding whether it *has* a baseline,
    and a missing or half-written report means the same thing to all of them.
    ``retryable_companies`` is the exception and reads the file itself, because
    "there is no report" is an answer a person asked for and must be told.
    """
    report_path = Path(report_path)
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", report_path, exc)
        return None
    return report if isinstance(report, dict) else None


def previous_job_counts(report_path: Path) -> dict[str, int]:
    """``{company: jobs collected}`` from a previous run's ``last_run.json``.

    Empty when there is no readable report - a first run has nothing to compare
    against, and that must not block its removal sync.

    Deliberately the *previous run's collected count*, not the stored row count.
    Comparing against the database would make the guard permanent: nothing is
    ever deleted, so the stored count never falls, so the collapse never clears.
    Comparing against the last run gives exactly one run of grace - long enough
    to absorb a transient traversal miss, short enough that a real, sustained
    halving is accepted on the next run and the removals go through.
    """
    report = _read_report(report_path)
    if not report:
        return {}

    counts: dict[str, int] = {}
    for row in report.get("companies") or []:
        name = str(row.get("company") or "").strip()
        jobs = row.get("jobs")
        if name and isinstance(jobs, int):
            counts[name] = jobs
    return counts


def collapsed_against(previous: int | None, collected: int) -> bool:
    """Whether ``collected`` is too far below ``previous`` to be believable."""
    if not previous or previous < COLLAPSE_FLOOR:
        return False
    return collected < previous * COLLAPSE_RATIO


def assign_change_status(jobs: list[dict[str, Any]], changed_ids: set[str]) -> None:
    """Label each row new / changed / unchanged, in place.

    "New" wins over "changed": a job seen for the first time has no previous
    state to have moved from, so reporting it as a change would be meaningless
    and would hide the more useful fact.

    Removed jobs are deliberately not a status here - this export lists what an
    employer is advertising now, and a row for a closed requisition is a link to
    a dead page. Removals are counted in the run summary instead.
    """
    for job in jobs:
        if job.get("is_new"):
            job["change_status"] = CHANGE_NEW
        elif job.get("job_id") in changed_ids:
            job["change_status"] = CHANGE_CHANGED
        else:
            job["change_status"] = CHANGE_UNCHANGED

FAILURE_FIELDS = [
    "company", "url", "ats_provider", "error_type", "error_message", "timestamp",
]


@dataclass
class RunSummary:
    """Counts reported at the end of a run."""

    #: Sortable identifier for this run, stamped onto every exported row.
    run_id: str = ""
    companies_scanned: int = 0
    companies_successful: int = 0
    companies_failed: int = 0
    #: A scrape that worked but stopped short of the provider's full job list.
    #: Distinct from failed: the rows are real, the coverage is not.
    companies_partial: int = 0
    #: Turned away by a bot challenge or an explicit denial. Distinct from
    #: failed because it needs a different route in, never a workaround.
    companies_blocked: int = 0
    jobs_collected: int = 0
    target_role_jobs: int = 0
    location_matches: int = 0
    within_window: int = 0
    date_unavailable: int = 0
    duplicates_removed: int = 0
    direct_api_companies: int = 0
    playwright_companies: int = 0
    fallback_companies: int = 0
    new_jobs: int = 0
    jobs_removed: int = 0
    discovered_ats_urls: int = 0
    ats_urls_written: int = 0
    hours_old: int = 72
    provider_counts: dict[str, int] = field(default_factory=dict)
    #: Companies whose scrape stopped short of the provider's full job list.
    incomplete_companies: int = 0
    #: Matching jobs whose title, location or URL moved since the last run.
    changed_jobs: int = 0
    #: ``(company, collected, reported_total, stop_reason)`` per truncated company.
    truncated: list[tuple[str, int, int | None, str | None]] = field(default_factory=list)
    #: Companies whose harvest collapsed against the previous run, so removal
    #: sync was withheld even though the collector claimed a complete walk.
    collapsed_companies: int = 0
    #: ``(company, collected, previous)`` per collapsed company.
    collapsed: list[tuple[str, int, int]] = field(default_factory=list)

    def as_digest_counts(self) -> dict[str, Any]:
        """The run's numbers as the digest wants them: four outcomes that do
        not overlap.

        ``companies_successful`` counts every company that was reached, which
        includes the truncated ones; ``companies_failed`` includes the blocked
        ones. Both are the right shape for the console summary and the wrong
        shape for a reader deciding whether to trust this digest, so the
        overlaps are subtracted out here rather than in the renderer.
        """
        return {
            "run_id": self.run_id,
            "companies_scanned": self.companies_scanned,
            "companies_successful": self.companies_successful - self.companies_partial,
            "companies_partial": self.companies_partial,
            "companies_failed": self.companies_failed - self.companies_blocked,
            "companies_blocked": self.companies_blocked,
            "jobs_collected": self.jobs_collected,
            "new_jobs": self.new_jobs,
            "changed_jobs": self.changed_jobs,
        }

    def render(self) -> str:
        lines = [
            "",
            "=" * 58,
            "  COMPANY ATS SCRAPER - RUN SUMMARY",
            "=" * 58,
            f"Run id:                 {self.run_id or '-'}",
            f"Companies scanned:      {self.companies_scanned:,}",
            f"Companies successful:   {self.companies_successful:,}",
            f"  of which partial:     {self.companies_partial:,}",
            f"Companies failed:       {self.companies_failed:,}",
            f"  of which blocked:     {self.companies_blocked:,}",
            "",
            f"Jobs collected:         {self.jobs_collected:,}",
            f"Target data jobs:       {self.target_role_jobs:,}",
            f"DFW/Remote matches:     {self.location_matches:,}",
            f"Within last {self.hours_old} hours:  {self.within_window:,}",
            f"Date unavailable:       {self.date_unavailable:,}",
            f"Duplicates removed:     {self.duplicates_removed:,}",
            f"Newly discovered:       {self.new_jobs:,}",
            f"Changed since last run: {self.changed_jobs:,}",
            f"Removed (no longer listed): {self.jobs_removed:,}",
            "",
            f"Direct API companies:   {self.direct_api_companies:,}",
            f"Playwright companies:   {self.playwright_companies:,}",
            f"Failed companies:       {self.companies_failed:,}",
        ]
        if self.fallback_companies:
            lines.append(f"API->browser fallbacks: {self.fallback_companies:,}")
        if self.discovered_ats_urls:
            lines.append(f"ATS discovered via search: {self.discovered_ats_urls:,}")
        if self.ats_urls_written:
            lines.append(f"ATS URLs written to Excel: {self.ats_urls_written:,}")
        if self.provider_counts:
            lines.extend(["", "Providers detected:"])
            for provider, count in sorted(
                self.provider_counts.items(), key=lambda kv: (-kv[1], kv[0])
            ):
                lines.append(f"  {provider:<18} {count:>4}")

        # Truncation used to be invisible: eleven Workday tenants all returned
        # exactly 500 jobs for months without anything saying so. Report it
        # loudly, sorted by how much was missed.
        if self.truncated:
            lines.extend([
                "",
                f"INCOMPLETE - {len(self.truncated)} company(ies) stopped short "
                f"(removal sync skipped for these):",
            ])
            for company, collected, total, reason in sorted(
                self.truncated, key=lambda row: -((row[2] or 0) - row[1])
            ):
                shortfall = f"{total - collected:,} missed" if total else "unknown shortfall"
                lines.append(
                    f"  {company[:26]:<26} {collected:>6,} of "
                    f"{(f'{total:,}' if total else '?'):>7}  {shortfall}  [{reason}]"
                )
        if self.collapsed:
            lines.extend([
                "",
                f"COLLAPSED - {len(self.collapsed)} company(ies) returned far "
                f"fewer jobs than last run (removal sync withheld):",
            ])
            for company, collected, previous in sorted(
                self.collapsed, key=lambda row: row[1] - row[2]
            ):
                lines.append(
                    f"  {company[:26]:<26} {collected:>6,} this run vs "
                    f"{previous:>6,} last run"
                )
        lines.append("=" * 58)
        return "\n".join(lines)


def resolve_companies_path(settings: Settings | None = None, excel_path: Path | str | None = None) -> Path:
    """The workbook path a run will read from (and, on a full run, write back to)."""
    cfg = settings or load_settings()
    return Path(excel_path) if excel_path else cfg.resolve_path("input_excel")


def load_companies(settings: Settings | None = None, excel_path: Path | str | None = None) -> pd.DataFrame:
    """Read the company workbook into a tidy DataFrame.

    Returns a frame with columns ``company``, ``ats_url``, ``live_jobs_url``.
    """
    cfg = settings or load_settings()
    path = resolve_companies_path(cfg, excel_path)
    if not path.exists():
        raise FileNotFoundError(f"Input workbook not found: {path}")

    sheet = cfg.get("input_sheet")
    frame = pd.read_excel(path, sheet_name=sheet if sheet else 0)

    columns = cfg.get("columns", {}) or {}
    company_col = columns.get("company", "Company")
    ats_col = columns.get("ats_url", "ATS URL")
    live_col = columns.get("live_jobs_url", "Live Jobs Page (if ATS URL unavailable)")

    missing = [c for c in (company_col, ats_col, live_col) if c not in frame.columns]
    if missing:
        raise ValueError(
            f"Workbook {path.name} is missing expected column(s): {missing}. "
            f"Found: {list(frame.columns)}"
        )

    tidy = frame[[company_col, ats_col, live_col]].copy()
    tidy.columns = ["company", "ats_url", "live_jobs_url"]
    tidy["company"] = tidy["company"].astype(str).str.strip()
    tidy = tidy[tidy["company"].notna() & (tidy["company"] != "") & (tidy["company"].str.lower() != "nan")]
    return tidy.reset_index(drop=True)


def filter_companies_by_name(companies: pd.DataFrame, needle: str) -> pd.DataFrame:
    """Rows whose company name contains ``needle`` (case-insensitive, literal).

    ``regex=False`` matters: company names routinely contain parentheses and
    periods ("Experis (ManpowerGroup)", "Robert Half (incl. ... Technology)")
    which are regex metacharacters - treating the needle as a pattern instead
    of literal text silently returns zero matches for names like those.
    """
    return companies[companies["company"].str.lower().str.contains(needle.strip().lower(), regex=False, na=False)]


def select_companies_by_name(
    companies: pd.DataFrame, names: Iterable[str]
) -> pd.DataFrame:
    """Rows whose company name is exactly one of ``names`` (case-insensitive).

    Distinct from :func:`filter_companies_by_name`, which is a substring search
    for interactive use. A retry works from names the pipeline itself wrote, so
    an exact match is both possible and safer - "Oracle" as a substring would
    also pull in any company whose name contains it.
    """
    wanted = {str(name).strip().lower() for name in names if str(name).strip()}
    return companies[companies["company"].str.strip().str.lower().isin(wanted)]


def build_plans(
    companies: pd.DataFrame,
    settings: Settings | None = None,
    *,
    resolve_pages: bool = True,
    workers: int | None = None,
    progress: Callable[[RoutePlan], None] | None = None,
) -> list[RoutePlan]:
    """Route every company, concurrently (page resolution is I/O bound)."""
    cfg = settings or load_settings()
    playwright_enabled = bool(cfg.get("playwright.enabled", True))
    max_workers = workers or int(cfg.get("concurrency.http_workers", 10))

    rows = companies.to_dict("records")
    plans: list[RoutePlan] = [None] * len(rows)  # type: ignore[list-item]

    def _route(index: int, row: dict[str, Any]) -> tuple[int, RoutePlan]:
        plan = plan_route(
            row["company"], row.get("ats_url"), row.get("live_jobs_url"),
            resolve_pages=resolve_pages, playwright_enabled=playwright_enabled,
        )
        return index, plan

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="route") as pool:
        futures = [pool.submit(_route, i, row) for i, row in enumerate(rows)]
        for future in as_completed(futures):
            try:
                index, plan = future.result()
            except Exception as exc:  # pragma: no cover - routing is defensive
                log.error("Routing failed unexpectedly: %s", exc)
                continue
            plans[index] = plan
            if progress:
                progress(plan)

    return [p for p in plans if p is not None]


def _teardown_pool_browsers(pool: ThreadPoolExecutor, workers: int, timeout: float = 60.0) -> None:
    """Close each worker thread's Playwright instance from within that thread.

    Submits one teardown task per worker, synchronised on a barrier so every
    worker thread runs exactly one (without the barrier a single fast thread
    could take all the tasks, leaving the others' browsers open).
    """
    try:
        from browser.playwright_scraper import shutdown_thread_browser
    except Exception as exc:  # pragma: no cover - browser never used
        log.debug("Browser teardown skipped: %s", exc)
        return

    barrier = threading.Barrier(workers, timeout=timeout)

    def _teardown() -> None:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            # Fewer live workers than expected; tear down anyway.
            pass
        shutdown_thread_browser()

    futures = [pool.submit(_teardown) for _ in range(workers)]
    for future in futures:
        try:
            future.result(timeout=timeout)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("Browser teardown task failed: %s", exc)


def _run_pool(

    runner: Callable[[RoutePlan], CompanyResult],
    plans: list[RoutePlan],
    *,
    workers: int,
    prefix: str,
    budget_seconds: float,
    company_timeout: float = 0.0,
    teardown_browsers: bool = False,
) -> list[CompanyResult]:
    """Run plans in a thread pool under per-company and per-phase time limits.

    A single wedged company must never stall the whole run. Playwright can
    block inside its own event loop with no timeout of its own - observed
    live: a career page whose overlay intercepted every click left one worker
    spinning in an actionability retry loop forever, and the main thread sat
    in ``as_completed`` indefinitely with ~100 orphaned Chromium processes.

    Two bounds, because one is not enough:

    * ``company_timeout`` is measured from when a company actually *starts*
      (not when it was submitted - with 3 workers and 70 companies most
      futures sit queued for a long time, and timing those from submission
      would fail them spuriously). This is what stops one bad page costing
      the whole phase.
    * ``budget_seconds`` caps the phase overall as a backstop.

    Anything unfinished at either bound is recorded as a Timeout failure, and
    the pool is abandoned without waiting (``shutdown(wait=False)``) so a
    stuck worker cannot block the process either.
    """
    results: list[CompanyResult] = []

    # Sized exactly to the concurrency limit, and deliberately so. Giving the
    # pool spare threads so an abandoned company could be "replaced" was tried
    # and reverted: a wedged thread keeps its Chromium instance - Playwright's
    # sync API is thread-affine, so no other thread can close it - and the
    # replacement then starts a *second* browser beside it. Six concurrent
    # instances against a measured ceiling of five took a 43-minute run with 3
    # failures to 3h13m with 19. The scarce resource is browsers, not threads.
    #
    # A wedged company is instead kept from doing damage by ordering (see
    # `_slowest_first`), not by trying to reclaim its slot.
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix)

    started: dict[str, float] = {}
    started_lock = threading.Lock()

    def _tracked(plan: RoutePlan) -> CompanyResult:
        with started_lock:
            started[plan.company] = time.monotonic()
        begin = time.monotonic()
        result = runner(plan)
        if result.duration_seconds is None:
            result.duration_seconds = time.monotonic() - begin
        return result

    futures = {pool.submit(_tracked, plan): plan for plan in plans}
    pending = set(futures)
    deadline = time.monotonic() + budget_seconds
    timed_out: set = set()

    while pending and time.monotonic() < deadline:
        finished, pending = wait(pending, timeout=5.0, return_when=FIRST_COMPLETED)

        for future in finished:
            plan = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # pragma: no cover - runner is defensive
                log.error("%s: unexpected executor error: %s", plan.company, exc)
                results.append(CompanyResult(
                    company=plan.company, jobs=[], plan=plan, success=False,
                    error_type=type(exc).__name__, error_message=str(exc),
                ))

        if not company_timeout:
            continue

        now = time.monotonic()
        overdue = set()
        with started_lock:
            for future in pending:
                begin = started.get(futures[future].company)
                if begin is not None and (now - begin) > company_timeout:
                    overdue.add(future)

        for future in overdue:
            plan = futures[future]
            log.error("%s: exceeded the %.0fs per-company limit; abandoning it",
                      plan.company, company_timeout)
            results.append(CompanyResult(
                company=plan.company, jobs=[], plan=plan, success=False,
                error_type="Timeout",
                error_message=f"Exceeded the {company_timeout:.0f}s per-company limit",
            ))
        pending -= overdue
        timed_out |= overdue

    not_done = pending

    if not_done:
        log.error(
            "%s phase hit its %.0fs budget with %s company(ies) unfinished; "
            "recording them as timeouts and moving on",
            prefix, budget_seconds, len(not_done),
        )
        for future in not_done:
            plan = futures[future]
            future.cancel()
            log.error("%s: timed out after %.0fs", plan.company, budget_seconds)
            results.append(CompanyResult(
                company=plan.company, jobs=[], plan=plan, success=False,
                error_type="Timeout",
                error_message=f"Exceeded the {prefix} phase budget of {budget_seconds:.0f}s",
            ))

    if teardown_browsers and not not_done and not timed_out:
        # Only safe when every worker is idle. A wedged worker cannot run a
        # teardown task, and waiting on one would reintroduce the hang - so
        # when anything timed out, leave the browsers to the process exit.
        _teardown_pool_browsers(pool, workers)

    # Never wait: a stuck worker would block here indefinitely.
    pool.shutdown(wait=False, cancel_futures=True)
    return results


def execute_plans(
    plans: Iterable[RoutePlan],
    settings: Settings | None = None,
) -> list[CompanyResult]:
    """Execute route plans, API and browser work in separate pools."""
    cfg = settings or load_settings()
    http_workers = int(cfg.get("concurrency.http_workers", 10))
    browser_workers = int(cfg.get("concurrency.playwright_workers", 3))
    playwright_enabled = bool(cfg.get("playwright.enabled", True))

    plan_list = list(plans)

    # Known problem companies run at the end of their phase, so whatever they
    # wedge, they wedge behind themselves. See :func:`slowest_last`.
    costs = previous_costs(cfg.resolve_path("output.directory", "output") / "last_run.json")
    api_plans = slowest_last([p for p in plan_list if p.method == METHOD_API], costs)
    browser_plans = slowest_last([p for p in plan_list if p.method == METHOD_BROWSER], costs)

    deferred = [p.company for p in (api_plans + browser_plans)
                if costs.get(p.company, (False, 0.0))[0]]
    log.info(
        "Executing %s companies: %s via direct API, %s via Playwright",
        len(plan_list), len(api_plans), len(browser_plans),
    )
    if deferred:
        log.info("Running last (timed out on the previous run): %s", ", ".join(deferred))

    results: list[CompanyResult] = []

    def _run(plan: RoutePlan) -> CompanyResult:
        return fetch_company_jobs(
            plan.company, plan=plan, playwright_enabled=playwright_enabled
        )

    # API companies first. Some will fall back to the browser internally; that
    # is bounded by the HTTP pool size, which is acceptable for the tail.
    if api_plans:
        results.extend(_run_pool(
            _run, api_plans, workers=http_workers, prefix="api",
            budget_seconds=float(cfg.get("concurrency.api_phase_timeout_seconds", 1800)),
            company_timeout=float(cfg.get("concurrency.api_company_timeout_seconds", 300)),
        ))

    if browser_plans and playwright_enabled:
        results.extend(_run_pool(
            _run, browser_plans, workers=browser_workers, prefix="browser",
            budget_seconds=float(cfg.get("concurrency.browser_phase_timeout_seconds", 2400)),
            company_timeout=float(cfg.get("concurrency.browser_company_timeout_seconds", 240)),
            teardown_browsers=True,
        ))
    elif browser_plans:
        for plan in browser_plans:
            results.append(CompanyResult(
                company=plan.company, jobs=[], plan=plan, success=False,
                error_type="PlaywrightDisabled",
                error_message="No direct collector and Playwright is disabled",
            ))

    try:
        from browser.playwright_scraper import shutdown_browsers
        shutdown_browsers()
    except Exception as exc:  # pragma: no cover - teardown best effort
        log.debug("Browser shutdown skipped: %s", exc)

    return results


#: Leading characters a spreadsheet treats as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_formulas(frame: pd.DataFrame) -> pd.DataFrame:
    """Neutralise spreadsheet formulas in scraped text before writing a CSV.

    Titles, locations and descriptions arrive verbatim from third-party pages,
    and this output exists to be opened in Excel or Sheets. A value starting
    ``=``, ``+``, ``-`` or ``@`` is evaluated as a formula on open, so a
    crafted job title becomes code running on the reader's machine.

    Prefixing with an apostrophe is the standard defence: the spreadsheet
    treats the cell as literal text and does not display the apostrophe, while
    the value stays readable to anything reading the CSV directly.
    """
    if frame.empty:
        return frame

    def _escape(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
            return f"'{value}"
        return value

    # Every column is walked and the isinstance check does the filtering. An
    # earlier version skipped columns whose dtype was not ``object``, which
    # silently disabled the whole guard under pandas 2.x - it infers ``str``
    # for text columns, so nothing was ever escaped.
    escaped = frame.copy()
    for column in escaped.columns:
        escaped[column] = escaped[column].map(_escape)
    return escaped


def write_outputs(
    jobs: list[dict[str, Any]],
    results: list[CompanyResult],
    settings: Settings | None = None,
    *,
    raw_jobs: list[dict[str, Any]] | None = None,
    prefix: str = "",
    run_id: str | None = None,
    carried_failures: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Path]:
    """Write company_jobs.csv / .json and scraper_failures.csv.

    ``prefix`` namespaces the filenames so a partial run (``--test-company``,
    ``--test-provider``, ``--limit``) cannot overwrite the outputs of a full
    run with its much smaller result set.

    ``run_id`` is stamped onto every row so a copy of the spreadsheet still
    says which run produced it. Pass ``None`` when the rows already carry the
    run that produced each of them - a merged export (see
    :func:`write_merged_outputs`) must not restamp rows it only carried over.

    ``carried_failures`` are failure rows from a previous run to keep
    alongside this one's. Only a merged export uses it: a retry never visits
    the blocked companies, and a failures file that quietly dropped them would
    read as "these are fixed now".
    """
    cfg = settings or load_settings()
    if run_id:
        for job in jobs:
            job["run_id"] = run_id
    out_dir = cfg.resolve_path("output.directory", "output")
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / f"{prefix}{cfg.get('output.csv', 'company_jobs.csv')}"
    json_path = out_dir / f"{prefix}{cfg.get('output.json', 'company_jobs.json')}"
    failures_path = out_dir / f"{prefix}{cfg.get('output.failures', 'scraper_failures.csv')}"

    jobs_frame = pd.DataFrame(jobs, columns=OUTPUT_FIELDS) if jobs else pd.DataFrame(columns=OUTPUT_FIELDS)
    if not jobs_frame.empty and "date_posted" in jobs_frame.columns:
        jobs_frame = jobs_frame.sort_values(
            "date_posted", ascending=False, na_position="last"
        ).reset_index(drop=True)

    jobs_frame = escape_formulas(jobs_frame)

    jobs_frame.to_csv(csv_path, index=False, encoding="utf-8")
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(jobs, handle, indent=2, ensure_ascii=False, default=str)

    timestamp = datetime.now(timezone.utc).isoformat()
    failure_rows = list(carried_failures or [])
    failure_rows.extend(
        {
            "company": result.company,
            "url": result.plan.url,
            "ats_provider": result.plan.provider,
            "error_type": result.error_type,
            "error_message": (result.error_message or "")[:500],
            "timestamp": timestamp,
        }
        for result in results
        if not result.success
    )
    pd.DataFrame(failure_rows, columns=FAILURE_FIELDS).to_csv(
        failures_path, index=False, encoding="utf-8"
    )

    written = {"csv": csv_path, "json": json_path, "failures": failures_path}

    # Excel alongside the CSV: it is what actually gets opened and mailed, and
    # openpyxl is already a dependency for the workbook.
    xlsx_name = cfg.get("output.xlsx", "company_jobs.xlsx")
    if xlsx_name:
        xlsx_path = out_dir / f"{prefix}{xlsx_name}"
        try:
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                jobs_frame.to_excel(writer, sheet_name="Matching jobs", index=False)
                if failure_rows:
                    pd.DataFrame(failure_rows, columns=FAILURE_FIELDS).to_excel(
                        writer, sheet_name="Failures", index=False
                    )
            written["xlsx"] = xlsx_path
        except Exception as exc:  # never fail a run over a spreadsheet
            log.warning("Could not write %s: %s", xlsx_path.name, exc)

    if raw_jobs is not None:
        raw_path = out_dir / f"{prefix}{cfg.get('output.raw_csv', 'company_jobs_raw.csv')}"
        pd.DataFrame(raw_jobs).to_csv(raw_path, index=False, encoding="utf-8")
        written["raw"] = raw_path

    return written


def read_export_rows(path: Path) -> list[dict[str, Any]]:
    """The job rows of a previous export, or ``[]`` if there is no usable one.

    The JSON export rather than the CSV: it is written from the same list and
    keeps ``job_id`` and real types, where a CSV round-trip would hand back
    every field as a string and lose the key the merge is built on.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s to merge into: %s", path, exc)
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def read_failure_rows(path: Path) -> list[dict[str, Any]]:
    """A previous ``scraper_failures.csv`` as row dicts, or ``[]``."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as exc:  # a truncated or half-written file is not fatal
        log.warning("Could not read %s to merge into: %s", path, exc)
        return []
    return frame.to_dict("records")


def write_merged_outputs(
    jobs: list[dict[str, Any]],
    results: list[CompanyResult],
    settings: Settings | None = None,
    *,
    raw_jobs: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    previous_counts: dict[str, int] | None = None,
) -> dict[str, Path]:
    """Write a partial run's rows *into* the full run's export files.

    Same filenames, same writer, same columns - only the row set differs, and
    only for the companies this run actually visited. Where there is no
    previous export to merge into (a first run, a cleared ``output/``) this is
    exactly :func:`write_outputs`, because an empty base merges to the rows
    this run produced.
    """
    cfg = settings or load_settings()
    out_dir = cfg.resolve_path("output.directory", "output")

    # Only this run's rows are stamped: a row carried over from the full run
    # keeps the run_id that actually produced it, which is the whole reason
    # the column exists.
    if run_id:
        for job in jobs:
            job["run_id"] = run_id

    previous_jobs = read_export_rows(out_dir / cfg.get("output.json", "company_jobs.json"))
    merged = merge_job_rows(previous_jobs, jobs, results, previous_counts)

    attempted = {result.company for result in results}
    carried = [
        row for row in read_failure_rows(
            out_dir / cfg.get("output.failures", "scraper_failures.csv")
        )
        if row.get("company") not in attempted
    ]

    log.info(
        "Merging %s row(s) from %s company(ies) into an export of %s -> %s row(s)",
        len(jobs), len(attempted), len(previous_jobs), len(merged),
    )
    return write_outputs(
        merged, results, cfg,
        raw_jobs=raw_jobs, prefix="", run_id=None, carried_failures=carried,
    )


def unannounced_matching_jobs(
    database: JobDatabase, final_jobs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Matching jobs that have never been announced as new.

    Deliberately *not* ``[j for j in final_jobs if j["is_new"]]``. ``is_new``
    is computed against the ids already in the database, and by the time a
    digest is attempted the jobs have been upserted - so a send that failed
    left those jobs no longer new on the next run, never back in the candidate
    set, and never announced. The notifications table faithfully recorded that
    they had not been sent, and nothing ever asked it.

    Asking the table directly makes the retry automatic: this run's new jobs
    and an earlier run's unannounced ones are the same query.
    """
    return database.filter_unnotified(final_jobs, kind="new")


#: Output kinds that may be mailed, best first. The spreadsheet is what a
#: person opens; the CSV is the machine-readable export and stands in only when
#: the workbook could not be written.
_ATTACHMENT_PREFERENCE = ("xlsx", "csv")


def select_attachments(
    paths: dict[str, Path], attach: bool = True
) -> list[Path]:
    """The one output file to attach to the digest, if any.

    Previously ``[p for k, p in paths.items() if k == "xlsx"]``, which mails a
    digest with no spreadsheet at all when the workbook could not be written -
    the case where the attachment matters most, since the reader then has only
    the links in the email body. The CSV carries the same rows, so it stands in.

    A path is only offered once it exists on disk: ``write_outputs`` records
    what it intended to write, and attaching a name whose file is missing costs
    the reader a warning instead of a file.
    """
    if not attach:
        return []
    for kind in _ATTACHMENT_PREFERENCE:
        path = paths.get(kind)
        if path and Path(path).exists():
            return [Path(path)]
    log.warning("No output file was available to attach to the digest")
    return []


def _prepare_notification(
    database: JobDatabase,
    summary: "RunSummary",
    final_jobs: list[dict[str, Any]],
    changed_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Work out what this run would announce, without announcing it yet.

    Split from the send so the decision is made while the database is open and
    the send happens after the outputs exist on disk - the attachment has to be
    written before it can be attached.
    """
    return {
        "new": unannounced_matching_jobs(database, final_jobs),
        "changed": database.filter_unnotified(changed_jobs, kind="changed"),
        # A run with any truncated company cannot be trusted to know what is
        # new - unless the only truncation was the job budget, which leaves a
        # gap of known shape. See notify.should_send.
        "run_complete": summary.incomplete_companies == 0,
        "stop_reasons": {r for _, _, _, r in summary.truncated if r},
        # How many companies - not just which reasons. One employer short of
        # its own reported total is not the same fact as thirty of them, and
        # the reason set alone cannot tell them apart.
        "untrustworthy_companies": sum(
            1 for _, _, _, reason in summary.truncated
            if reason and reason not in DESCRIBABLE_STOP_REASONS
        ),
    }


def send_notifications(
    payload: dict[str, Any],
    summary: "RunSummary",
    database_path: Path,
    settings: Settings,
    attachments: Iterable[Path] = (),
) -> bool:
    """Send the digest, then record what was announced.

    Jobs are marked notified **only after** a successful send: doing it before
    would let one SMTP failure suppress those jobs permanently.
    """
    from notify import build_digest, load_email_config, send_digest, should_send

    new_jobs, changed_jobs = payload["new"], payload["changed"]
    if not should_send(
        new_jobs=new_jobs, changed_jobs=changed_jobs,
        run_complete=payload["run_complete"],
        stop_reasons=payload.get("stop_reasons"),
        untrustworthy_companies=payload.get("untrustworthy_companies"),
    ):
        log.info("Nothing new to announce; no email sent")
        return False

    config = load_email_config(settings.get("notifications.email"))
    if config is None:
        log.info(
            "%s new and %s changed job(s) to announce, but email is not "
            "configured; skipping send", len(new_jobs), len(changed_jobs),
        )
        return False

    digest = build_digest(new_jobs, changed_jobs, summary.as_digest_counts())

    if not send_digest(config, digest, attachments):
        return False

    # A dry run rendered the digest to disk without mailing anyone. Recording
    # those jobs as announced would mean the first *real* send silently skipped
    # everything a preview had already seen.
    if config.dry_run:
        log.info("Dry run: %s new and %s changed job(s) left unmarked so a real "
                 "send still announces them", len(new_jobs), len(changed_jobs))
        return True

    with JobDatabase(database_path) as database:
        database.record_notified(new_jobs, kind="new")
        database.record_notified(changed_jobs, kind="changed")
    return True


def sync_completed_companies(
    results: Iterable[CompanyResult],
    database: JobDatabase,
    previous_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    """Upsert each company's jobs and age out the ones it no longer lists.

    Four conditions must all hold before a company is synced, and each guards
    a different way of misreading absence as closure:

    * ``result.success`` - a company we could not reach tells us nothing.
    * ``result.jobs`` - an empty harvest is not evidence every posting closed.
    * ``result.complete`` - a scrape that stopped partway through pagination
      never saw the later pages, so the jobs on them are missing from *our*
      data, not from the employer's site. This is the condition that was absent
      before :class:`ats.base.CollectionResult` existed, and it is why one
      transient HTTP error could delete hundreds of live postings.
    * **the harvest did not collapse** against the previous run. ``complete``
      is the collector's own account of itself, and it is only as good as the
      collector's knowledge: a browser traversal that lands on a "featured
      roles" panel instead of the job list returns four rows and believes it
      saw everything. Nothing inside that scrape can tell it otherwise - but
      the previous run can. See :data:`COLLAPSE_RATIO`.

    Args:
        previous_counts: ``{company: jobs}`` from the previous run's report.
            Omitted or empty disables collapse detection, so a first run
            (which has nothing to compare against) syncs normally.

    Returns:
        ``{"removed": int, "synced": int, "skipped_incomplete": int,
        "skipped_collapsed": int}``
    """
    stats: dict[str, Any] = {
        "removed": 0, "synced": 0, "skipped_incomplete": 0,
        "skipped_collapsed": 0, "collapsed": [],
    }
    previous_counts = previous_counts or {}

    for result in results:
        if not result.success or not result.jobs:
            continue
        database.upsert_jobs(result.jobs)

        if not result.complete:
            stats["skipped_incomplete"] += 1
            log.warning(
                "%s: scrape incomplete (%s) - upserted %s job(s) but skipping "
                "removal sync so unreached postings are not deleted",
                result.company, result.stop_reason or "unknown", len(result.jobs),
            )
            continue

        previous = previous_counts.get(result.company)
        if collapsed_against(previous, len(result.jobs)):
            stats["skipped_collapsed"] += 1
            stats["collapsed"].append((result.company, len(result.jobs), int(previous)))
            log.warning(
                "%s: collected %s job(s) after %s last run - too steep a drop "
                "to read as closures, so removal sync is skipped. If the next "
                "run agrees, the removals go through then.",
                result.company, len(result.jobs), previous,
            )
            continue

        ids = {j["job_id"] for j in result.jobs if j.get("job_id")}
        stats["removed"] += database.sync_company(result.company, ids)["removed"]
        stats["synced"] += 1

    return stats


def verified_repair(result: CompanyResult) -> tuple[str, str, str] | None:
    """``(source, raw_url, repaired_url)`` if this result should overwrite a
    dead workbook URL with the live one url_repair.py found, else ``None``.

    A repair only qualifies once it is verified by actually returning jobs
    this run - never a URL that merely "looks like a careers page".

    Skipped only when the repair was superseded by a further page-resolved
    ATS discovery *from a blank Live Jobs Page source* (Primoris, Cotality):
    :func:`write_discovered_urls` already wrote the better, more specific URL
    into the blank ``ATS URL`` column, and writing it here too would put an
    ATS endpoint into the ``Live Jobs Page`` column instead. When the dead
    value being replaced was itself in ``ATS URL`` (JPS Health Network: a
    dead ATS URL that got repaired and then resolved to its real Cornerstone
    endpoint), the improved URL belongs in that same column, so this still
    applies.
    """
    plan = result.plan
    if not (result.success and plan.was_repaired and plan.raw_url and plan.url):
        return None
    if plan.url == plan.raw_url:
        return None
    if plan.resolved_via_page and plan.source == SOURCE_LIVE_PAGE:
        return None
    return (plan.source, plan.raw_url, plan.url)


def speaks_for_whole_workbook(output_prefix: str, merge_into_full: bool) -> bool:
    """May this run act on companies it never visited?

    Only a run that saw every company may send the digest or write a retrieval
    status back to the workbook - a slice would mark every company it skipped
    as FALSE, which is a lie rather than a gap. That used to be "the output
    prefix is empty", which stopped being the same question the moment a
    merged retry started writing unprefixed files.
    """
    return not output_prefix and not merge_into_full


def run(
    settings: Settings | None = None,
    *,
    excel_path: Path | str | None = None,
    company_filter: str | None = None,
    company_names: Iterable[str] | None = None,
    provider_filter: str | None = None,
    limit: int | None = None,
    resolve_pages: bool = True,
    save_raw: bool = False,
    output_prefix: str = "",
    merge_into_full: bool = False,
    write_back: bool = True,
    notify: bool = True,
) -> tuple[RunSummary, list[dict[str, Any]], list[CompanyResult]]:
    """Execute a full scrape and write outputs.

    ``merge_into_full`` writes a partial run's rows into the unprefixed
    full-run export and report instead of a namespaced copy, per company - see
    :func:`merge_job_rows`. It is not a full run, so the side effects that only
    a full run may have (the digest, the workbook write-back) stay off.

    Returns ``(summary, final_jobs, company_results)``.
    """
    cfg = settings or load_settings()
    full_run = speaks_for_whole_workbook(output_prefix, merge_into_full)
    run_id = new_run_id()
    log.info("Run %s starting", run_id)
    companies = load_companies(cfg, excel_path)

    if company_filter:
        companies = filter_companies_by_name(companies, company_filter)
        if companies.empty:
            raise ValueError(f"No company in the workbook matches {company_filter!r}")

    if company_names is not None:
        wanted = list(company_names)
        companies = select_companies_by_name(companies, wanted)
        if companies.empty:
            raise ValueError(
                f"None of the {len(wanted)} named company(ies) are in the workbook"
            )
        log.info("Retrying %s of %s named company(ies)", len(companies), len(wanted))

    if limit:
        companies = companies.head(limit)

    log.info("Routing %s companies...", len(companies))
    plans = build_plans(companies, cfg, resolve_pages=resolve_pages)

    if provider_filter:
        wanted = provider_filter.strip().lower()
        plans = [p for p in plans if p.provider.lower() == wanted]
        if not plans:
            raise ValueError(f"No companies routed to provider {provider_filter!r}")
        log.info("Filtered to %s companies on provider %r", len(plans), provider_filter)

    results = execute_plans(plans, cfg)

    summary = RunSummary(
        companies_scanned=len(plans), hours_old=int(cfg.get("hours_old", 72))
    )
    all_jobs: list[dict[str, Any]] = []

    for result in results:
        status = company_status(result)
        if status == STATUS_PARTIAL:
            summary.companies_partial += 1
        elif status == STATUS_BLOCKED:
            summary.companies_blocked += 1

        if result.success:
            summary.companies_successful += 1
        else:
            summary.companies_failed += 1
            log.error("%s -> %s: %s", result.company, result.error_type, result.error_message)

        if result.fell_back:
            summary.fallback_companies += 1
        if result.plan.method == METHOD_BROWSER:
            summary.playwright_companies += 1
        else:
            summary.direct_api_companies += 1

        if result.success and not result.complete:
            summary.truncated.append((
                result.company, len(result.jobs),
                result.reported_total, result.stop_reason,
            ))

        provider = result.plan.provider
        summary.provider_counts[provider] = summary.provider_counts.get(provider, 0) + 1
        all_jobs.extend(result.jobs)

    # job_id is a database-layer identity, never part of the spec'd normalized
    # record - computed once here and carried alongside each dict, but never
    # written into RECORD_FIELDS/OUTPUT_FIELDS (see write_outputs()).
    # Scoped by company: the extracted ids are only unique *within* an
    # employer, and job_id is the jobs-table primary key.
    for job in all_jobs:
        job["job_id"] = extract_stable_job_id(
            job.get("job_url"), job.get("ats_provider"), job.get("company"),
        )

    summary.jobs_collected = len(all_jobs)
    log.info("Collected %s raw jobs across %s companies", len(all_jobs), len(results))

    db_path = cfg.resolve_path("database_path", "data/jobs.db")
    # Read before write_run_report() overwrites it at the end of this run.
    previous_counts = previous_job_counts(
        cfg.resolve_path("output.directory", "output") / f"{output_prefix}last_run.json"
    )
    with JobDatabase(db_path) as database:
        known_before = database.known_ids()
        first_seen = database.get_first_seen_map([j.get("job_id") for j in all_jobs])

        filtered = apply_filters(
            all_jobs, cfg, first_seen_lookup=first_seen, enricher=enrich_records
        )
        counts = filtered["counts"]
        summary.target_role_jobs = counts["target_role"]
        summary.location_matches = counts["location_match"]
        summary.within_window = counts["within_window"]
        summary.date_unavailable = counts["date_unavailable"]

        # Explainable fit scoring, on the filtered set only - it reads
        # descriptions, and most collected jobs never reach the output.
        for job in filtered["jobs"]:
            job.update(score_fit(job, cfg).as_dict())

        deduped = deduplicate(filtered["jobs"])
        summary.duplicates_removed = deduped["removed"]
        final_jobs = deduped["jobs"]

        # Per-company upsert + removal sync, gated on success AND completeness.
        # See sync_completed_companies() for why both matter.
        sync_stats = sync_completed_companies(results, database, previous_counts)
        summary.jobs_removed = sync_stats["removed"]
        summary.incomplete_companies = sync_stats["skipped_incomplete"]
        summary.collapsed_companies = sync_stats["skipped_collapsed"]
        summary.collapsed = list(sync_stats["collapsed"])

        refreshed = database.get_first_seen_map([j.get("job_id") for j in final_jobs])
        for job in final_jobs:
            job_id = job.get("job_id", "")
            job["first_seen"] = refreshed.get(job_id)
            job["is_new"] = job_id not in known_before

        summary.new_jobs = sum(1 for job in final_jobs if job.get("is_new"))

        # Changes are reported only for jobs that survived filtering - a
        # retitled warehouse role is a change, but not one worth an email.
        matching_ids = {j.get("job_id") for j in final_jobs}
        changed_jobs = [
            c for c in database.changed_since_last_run()
            if c["job_id"] in matching_ids
        ]
        summary.changed_jobs = len(changed_jobs)
        assign_change_status(final_jobs, {c["job_id"] for c in changed_jobs})
        database.clear_change_marks()

        notify_payload = _prepare_notification(
            database, summary, final_jobs, changed_jobs,
        )

    if merge_into_full:
        paths = write_merged_outputs(
            final_jobs, results, cfg,
            raw_jobs=all_jobs if save_raw else None, run_id=run_id,
            previous_counts=previous_counts,
        )
    else:
        paths = write_outputs(
            final_jobs, results, cfg,
            raw_jobs=all_jobs if save_raw else None, prefix=output_prefix,
            run_id=run_id,
        )
    # Written after the job files so a reader who sees last_run.json can trust
    # that the spreadsheet it describes is already on disk.
    paths["report"] = write_run_report(
        summary, results, cfg.resolve_path("output.directory", "output"),
        run_id, prefix=output_prefix, previous_counts=previous_counts,
        merge_into_previous=merge_into_full,
    )
    summary.run_id = run_id
    for label, path in paths.items():
        log.info("Wrote %s -> %s", label, path)

    # Sent after the outputs exist, so the spreadsheet can be attached. Only on
    # a full run: a --limit or --test-company slice knows nothing about the
    # companies it skipped, so its "new" set is not a real answer.
    if notify and full_run:
        # notifications.email.attach_spreadsheet was defined in settings.yaml
        # and never read - the workbook was attached unconditionally, so
        # turning it off had no effect.
        attach = bool(cfg.get("notifications.email.attach_spreadsheet", True))
        attachments = select_attachments(paths, attach)
        send_notifications(notify_payload, summary, db_path, cfg, attachments)

    # Search-fallback ATS discovery, written back so the next run routes these
    # companies straight to a direct-API collector instead of Playwright.
    # Only on a full run (never a --test-company/--test-provider/--limit
    # partial run, matching the same distinction output_prefix already makes).
    # Only verified discoveries - ones whose collector actually returned jobs
    # this run - are written back. A URL that merely pattern-matches an ATS
    # would otherwise poison next run's routing.
    discoveries = {
        result.company: result.discovered_ats_url
        for result in results
        if result.discovered_ats_url and result.discovery_verified
    }
    summary.discovered_ats_urls = len(discoveries)

    if discoveries and write_back and full_run:
        companies_path = resolve_companies_path(cfg, excel_path)
        export_result = write_discovered_urls(companies_path, discoveries)
        summary.ats_urls_written = export_result["updated"]

    # A dead workbook URL that url_repair.py swapped for a live one this run,
    # written back so the next run starts from the live URL instead of
    # re-repairing the same dead one every time - see verified_repair().
    repairs = {
        result.company: repair
        for result in results
        if (repair := verified_repair(result)) is not None
    }

    if repairs and write_back and full_run:
        companies_path = resolve_companies_path(cfg, excel_path)
        repair_result = write_repaired_urls(companies_path, repairs)
        summary.ats_urls_written += repair_result["updated"]

    # Per-company retrieval outcome, so the workbook itself shows which
    # companies this pipeline can actually reach. Scoped to full runs for the
    # same reason as the ATS write-back: a --limit run would mark every
    # unvisited company FALSE, which would be a lie rather than a gap.
    if write_back and full_run:
        counts = {
            result.company: (len(result.jobs) if result.success else 0)
            for result in results
        }
        companies_path = resolve_companies_path(cfg, excel_path)
        write_run_status(companies_path, counts)

    return summary, final_jobs, results
