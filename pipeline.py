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
    CompanyResult,
    RoutePlan,
    fetch_company_jobs,
    plan_route,
)
from database import JobDatabase
from deduplicate import deduplicate
from enrich import enrich_records
from export_ats_urls import write_discovered_urls
from filters import DATE_UNAVAILABLE, WITHIN_WINDOW, apply_filters
from job_identity import extract_stable_job_id
from logger import get_logger
from normalize import RECORD_FIELDS
from settings import Settings, load_settings

log = get_logger("pipeline")

OUTPUT_FIELDS = list(RECORD_FIELDS) + [
    "date_filter_status",
    "location_match_type",
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
    provider_counts: dict[str, int] = field(default_factory=dict)

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
            f"Within last 72 hours:   {self.within_window:,}",
            f"Date unavailable:       {self.date_unavailable:,}",
            f"Duplicates removed:     {self.duplicates_removed:,}",
            f"Newly discovered:       {self.new_jobs:,}",
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

    if raw_jobs is not None:
        raw_path = out_dir / f"{prefix}{cfg.get('output.raw_csv', 'company_jobs_raw.csv')}"
        pd.DataFrame(raw_jobs).to_csv(raw_path, index=False, encoding="utf-8")
        written["raw"] = raw_path

    return written


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
) -> tuple[RunSummary, list[dict[str, Any]], list[CompanyResult]]:
    """Execute a full scrape and write outputs.

    Returns ``(summary, final_jobs, company_results)``.
    """
    cfg = settings or load_settings()
    companies = load_companies(cfg, excel_path)

    if company_filter:
        needle = company_filter.strip().lower()
        companies = companies[companies["company"].str.lower().str.contains(needle, na=False)]
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

    summary = RunSummary(companies_scanned=len(plans))
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

        provider = result.plan.provider
        summary.provider_counts[provider] = summary.provider_counts.get(provider, 0) + 1
        all_jobs.extend(result.jobs)

    # job_id is a database-layer identity, never part of the spec'd normalized
    # record - computed once here and carried alongside each dict, but never
    # written into RECORD_FIELDS/OUTPUT_FIELDS (see write_outputs()).
    for job in all_jobs:
        job["job_id"] = extract_stable_job_id(job.get("job_url"), job.get("ats_provider"))

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

        deduped = deduplicate(filtered["jobs"])
        summary.duplicates_removed = deduped["removed"]
        final_jobs = deduped["jobs"]

        # Per-company upsert + removal sync. Only for companies scraped
        # successfully this run - a failed company's jobs must never be
        # deleted just because this run couldn't reach its page (that would
        # read as "all jobs closed" when it was really a scraping hiccup).
        # sync_company() only ever touches that one company's rows, via
        # idx_jobs_company - never a full-table scan.
        for result in results:
            if not result.success or not result.jobs:
                continue
            database.upsert_jobs(result.jobs)
            ids = {j["job_id"] for j in result.jobs if j.get("job_id")}
            sync_stats = database.sync_company(result.company, ids)
            summary.jobs_removed += sync_stats["removed"]

        refreshed = database.get_first_seen_map([j.get("job_id") for j in final_jobs])
        for job in final_jobs:
            job_id = job.get("job_id", "")
            job["first_seen"] = refreshed.get(job_id)
            job["is_new"] = job_id not in known_before

        summary.new_jobs = sum(1 for job in final_jobs if job.get("is_new"))

    paths = write_outputs(
        final_jobs, results, cfg,
        raw_jobs=all_jobs if save_raw else None, prefix=output_prefix,
    )
    for label, path in paths.items():
        log.info("Wrote %s -> %s", label, path)

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

    return summary, final_jobs, results
