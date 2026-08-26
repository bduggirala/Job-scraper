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
from typing import Any, Callable, Iterable

import pandas as pd

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
    "location_match_type",
    "remote_scope",
    "source_query",
    "fit_score",
    "fit_matched",
    "fit_explanation",
    "first_seen",
    "is_new",
]

FAILURE_FIELDS = [
    "company", "url", "ats_provider", "error_type", "error_message", "timestamp",
]


@dataclass
class RunSummary:
    """Counts reported at the end of a run."""

    companies_scanned: int = 0
    companies_successful: int = 0
    companies_failed: int = 0
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

    def render(self) -> str:
        lines = [
            "",
            "=" * 58,
            "  COMPANY ATS SCRAPER - RUN SUMMARY",
            "=" * 58,
            f"Companies scanned:      {self.companies_scanned:,}",
            f"Companies successful:   {self.companies_successful:,}",
            f"Companies failed:       {self.companies_failed:,}",
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
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix)

    started: dict[str, float] = {}
    started_lock = threading.Lock()

    def _tracked(plan: RoutePlan) -> CompanyResult:
        with started_lock:
            started[plan.company] = time.monotonic()
        return runner(plan)

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

    done = set(futures) - pending - timed_out
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
    api_plans = [p for p in plan_list if p.method == METHOD_API]
    browser_plans = [p for p in plan_list if p.method == METHOD_BROWSER]

    log.info(
        "Executing %s companies: %s via direct API, %s via Playwright",
        len(plan_list), len(api_plans), len(browser_plans),
    )

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
) -> dict[str, Path]:
    """Write company_jobs.csv / .json and scraper_failures.csv.

    ``prefix`` namespaces the filenames so a partial run (``--test-company``,
    ``--test-provider``, ``--limit``) cannot overwrite the outputs of a full
    run with its much smaller result set.
    """
    cfg = settings or load_settings()
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
    failure_rows = [
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
    ]
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
    new_jobs = [j for j in final_jobs if j.get("is_new")]
    return {
        "new": database.filter_unnotified(new_jobs, kind="new"),
        "changed": database.filter_unnotified(changed_jobs, kind="changed"),
        # A run with any truncated company cannot be trusted to know what is
        # new, so the digest is suppressed rather than sent with a caveat.
        "run_complete": summary.incomplete_companies == 0,
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

    digest = build_digest(new_jobs, changed_jobs, {
        "companies_scanned": summary.companies_scanned,
        "jobs_collected": summary.jobs_collected,
        "incomplete_companies": summary.incomplete_companies,
    })

    if not send_digest(config, digest, attachments):
        return False

    with JobDatabase(database_path) as database:
        database.record_notified(new_jobs, kind="new")
        database.record_notified(changed_jobs, kind="changed")
    return True


def sync_completed_companies(
    results: Iterable[CompanyResult], database: JobDatabase
) -> dict[str, int]:
    """Upsert each company's jobs and age out the ones it no longer lists.

    Three conditions must all hold before a company is synced, and each guards
    a different way of misreading absence as closure:

    * ``result.success`` - a company we could not reach tells us nothing.
    * ``result.jobs`` - an empty harvest is not evidence every posting closed.
    * ``result.complete`` - a scrape that stopped partway through pagination
      never saw the later pages, so the jobs on them are missing from *our*
      data, not from the employer's site. This is the condition that was absent
      before :class:`ats.base.CollectionResult` existed, and it is why one
      transient HTTP error could delete hundreds of live postings.

    Returns:
        ``{"removed": int, "synced": int, "skipped_incomplete": int}``
    """
    stats = {"removed": 0, "synced": 0, "skipped_incomplete": 0}

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


def run(
    settings: Settings | None = None,
    *,
    excel_path: Path | str | None = None,
    company_filter: str | None = None,
    provider_filter: str | None = None,
    limit: int | None = None,
    resolve_pages: bool = True,
    save_raw: bool = False,
    output_prefix: str = "",
    write_back: bool = True,
    notify: bool = True,
) -> tuple[RunSummary, list[dict[str, Any]], list[CompanyResult]]:
    """Execute a full scrape and write outputs.

    Returns ``(summary, final_jobs, company_results)``.
    """
    cfg = settings or load_settings()
    companies = load_companies(cfg, excel_path)

    if company_filter:
        companies = filter_companies_by_name(companies, company_filter)
        if companies.empty:
            raise ValueError(f"No company in the workbook matches {company_filter!r}")

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
        sync_stats = sync_completed_companies(results, database)
        summary.jobs_removed = sync_stats["removed"]
        summary.incomplete_companies = sync_stats["skipped_incomplete"]

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
        database.clear_change_marks()

        notify_payload = _prepare_notification(
            database, summary, final_jobs, changed_jobs,
        )

    paths = write_outputs(
        final_jobs, results, cfg,
        raw_jobs=all_jobs if save_raw else None, prefix=output_prefix,
    )
    for label, path in paths.items():
        log.info("Wrote %s -> %s", label, path)

    # Sent after the outputs exist, so the spreadsheet can be attached. Only on
    # a full run: a --limit or --test-company slice knows nothing about the
    # companies it skipped, so its "new" set is not a real answer.
    if notify and not output_prefix:
        attachments = [p for k, p in paths.items() if k == "xlsx"]
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

    if discoveries and write_back and not output_prefix:
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

    if repairs and write_back and not output_prefix:
        companies_path = resolve_companies_path(cfg, excel_path)
        repair_result = write_repaired_urls(companies_path, repairs)
        summary.ats_urls_written += repair_result["updated"]

    # Per-company retrieval outcome, so the workbook itself shows which
    # companies this pipeline can actually reach. Scoped to full runs for the
    # same reason as the ATS write-back: a --limit run would mark every
    # unvisited company FALSE, which would be a lie rather than a gap.
    if write_back and not output_prefix:
        counts = {
            result.company: (len(result.jobs) if result.success else 0)
            for result in results
        }
        companies_path = resolve_companies_path(cfg, excel_path)
        write_run_status(companies_path, counts)

    return summary, final_jobs, results
