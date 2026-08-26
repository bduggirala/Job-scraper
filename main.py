"""CLI entry point for the company ATS scraper.

    python main.py                              # full run
    python main.py --dry-run                    # routing decisions only
    python main.py --test-company "Fidelity Investments"
    python main.py --test-provider workday

This pipeline is independent of the JobSpy scraper: it reads its own workbook,
writes its own outputs, and deduplicates only against itself.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

from ats.router import METHOD_API, METHOD_BROWSER
from logger import get_logger, setup_logging
from pipeline import (
    build_plans,
    filter_companies_by_name,
    load_companies,
    retryable_companies,
    run,
)
from settings import load_settings

log = get_logger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Scrape jobs directly from company ATS systems and career pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py\n'
            '  python main.py --dry-run\n'
            '  python main.py --test-company "Fidelity Investments"\n'
            '  python main.py --test-provider workday\n'
        ),
    )
    parser.add_argument("--config", default=None, help="Path to settings.yaml")
    parser.add_argument("--excel", default=None, help="Override the input workbook path")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read the workbook, detect ATS providers, print routing decisions, scrape nothing",
    )
    parser.add_argument(
        "--resolve", action="store_true",
        help="With --dry-run, also fetch branded pages to resolve their ATS (one GET per company)",
    )
    parser.add_argument(
        "--test-company", metavar="NAME",
        help="Scrape only companies whose name contains NAME, with detailed diagnostics",
    )
    parser.add_argument(
        "--test-provider", metavar="PROVIDER",
        help="Scrape only companies routed to PROVIDER (e.g. workday, greenhouse)",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Re-run only the companies the last run recorded as failed or partial "
             "(from output/last_run.json). Blocked companies are skipped.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N companies")
    parser.add_argument(
        "--no-playwright", action="store_true",
        help="Disable the browser fallback; unresolvable companies are recorded as failures",
    )
    parser.add_argument(
        "--no-resolve", action="store_true",
        help="Skip page-level ATS resolution; unknown URLs go straight to Playwright",
    )
    parser.add_argument(
        "--save-raw", action="store_true",
        help="Also write every collected job (pre-filter) to output/company_jobs_raw.csv",
    )
    parser.add_argument(
        "--no-write-back", action="store_true",
        help="Don't write ATS URLs discovered via the search fallback back into the workbook",
    )
    parser.add_argument(
        "--no-email", action="store_true",
        help="Don't send the email digest, even on a full run",
    )
    parser.add_argument("--quiet", action="store_true", help="Log to file only")
    return parser


def cmd_dry_run(args: argparse.Namespace, settings) -> int:
    """Print routing decisions without scraping."""
    companies = load_companies(settings, args.excel)
    if args.test_company:
        companies = filter_companies_by_name(companies, args.test_company)
        if companies.empty:
            print(f"No company matches {args.test_company!r}")
            return 1
    if args.limit:
        companies = companies.head(args.limit)

    resolve = args.resolve and not args.no_resolve
    print(f"\nRouting {len(companies)} companies "
          f"({'with' if resolve else 'without'} page resolution)...\n")

    plans = build_plans(companies, settings, resolve_pages=resolve)

    if args.test_provider:
        wanted = args.test_provider.strip().lower()
        plans = [p for p in plans if p.provider.lower() == wanted]

    by_company = {p.company: p for p in plans}
    for company in companies["company"]:
        plan = by_company.get(company)
        if plan:
            print(f"  {plan.describe()}")

    api_count = sum(1 for p in plans if p.method == METHOD_API)
    browser_count = sum(1 for p in plans if p.method == METHOD_BROWSER)
    resolved = sum(1 for p in plans if p.resolved_via_page)

    providers: dict[str, int] = {}
    for plan in plans:
        providers[plan.provider] = providers.get(plan.provider, 0) + 1

    print("\n" + "=" * 58)
    print("  DRY RUN - ROUTING PLAN")
    print("=" * 58)
    print(f"Companies routed:       {len(plans):,}")
    print(f"Direct API companies:   {api_count:,}")
    print(f"Playwright companies:   {browser_count:,}")
    if resolve:
        print(f"Resolved from page:     {resolved:,}")
    print("\nProviders detected:")
    for provider, count in sorted(providers.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {provider:<18} {count:>4}")
    print("=" * 58)
    print("\nNo jobs were scraped (--dry-run).")
    return 0


def cmd_scrape(args: argparse.Namespace, settings) -> int:
    """Run the full pipeline."""
    diagnostic = bool(args.test_company or args.test_provider)

    names = None
    if args.retry_failed:
        report = settings.resolve_path("output.directory", "output") / "last_run.json"
        names = retryable_companies(report)
        if not names:
            print(f"\nNothing to retry: no failed or partial company in {report}")
            return 0
        print(f"\nRetrying {len(names)} company(ies) from {report}:")
        for name in names:
            print(f"  {name}")

    # A partial run must not overwrite a full run's outputs. A retry is one:
    # it knows nothing about the companies that already succeeded, so its
    # spreadsheet is a slice, not a replacement.
    partial = diagnostic or bool(args.limit) or bool(args.retry_failed)
    prefix = "retry_" if args.retry_failed and not diagnostic else (
        "test_" if partial else ""
    )

    summary, jobs, results = run(
        settings,
        excel_path=args.excel,
        company_filter=args.test_company,
        company_names=names,
        provider_filter=args.test_provider,
        limit=args.limit,
        resolve_pages=not args.no_resolve,
        save_raw=args.save_raw,
        output_prefix=prefix,
        write_back=not args.no_write_back,
        notify=not args.no_email,
    )

    if diagnostic:
        print("\n" + "=" * 58)
        print("  DIAGNOSTICS")
        print("=" * 58)
        for result in results:
            plan = result.plan
            status = "OK" if result.success else f"FAILED ({result.error_type})"
            print(f"\n{result.company}")
            print(f"  URL:          {plan.url}")
            print(f"  Provider:     {plan.provider}")
            print(f"  Method:       {plan.method}"
                  f"{' (fell back from API)' if result.fell_back else ''}")
            print(f"  Source column:{plan.source}")
            print(f"  Resolved:     {plan.resolved_via_page}")
            print(f"  Status:       {status}")
            print(f"  Jobs found:   {len(result.jobs)}")
            if result.discovered_provider:
                print(f"  Discovered:   {result.discovered_provider} ({result.discovered_ats_url})")
            if result.error_message:
                print(f"  Error:        {result.error_message[:300]}")
            for job in result.jobs[:5]:
                print(f"    - {job['title']} | {job.get('location')} | {job.get('date_posted')}")
            if len(result.jobs) > 5:
                print(f"    ... and {len(result.jobs) - 5} more")

    print(summary.render())

    if jobs:
        print(f"\nTop matches ({min(len(jobs), 15)} of {len(jobs)}):")
        for job in jobs[:15]:
            flag = "NEW " if job.get("is_new") else "    "
            print(f"  {flag}{job['company']:<28} | {job['title'][:52]:<52} "
                  f"| {(job.get('location') or '-')[:28]:<28} | {job['date_filter_status']}")
    else:
        print("\nNo jobs matched the role + location + freshness filters this run.")

    out_dir = settings.resolve_path("output.directory", "output")
    if prefix:
        print(f"\nPartial run - outputs written to: {out_dir} (prefixed '{prefix}')")
        print("Full-run outputs were left untouched.")
        if args.retry_failed:
            print("Re-run the full workbook to refresh output/company_jobs.xlsx.")
    else:
        print(f"\nOutputs written to: {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        settings.resolve_path("logging.file", "logs/scraper.log"),
        level=settings.get("logging.level", "INFO"),
        quiet=args.quiet,
    )

    if args.no_playwright:
        settings._data.setdefault("playwright", {})["enabled"] = False  # noqa: SLF001

    try:
        if args.dry_run:
            return cmd_dry_run(args, settings)
        return cmd_scrape(args, settings)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        print(f"\nError: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        log.error("%s", exc)
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def _exit(code: int, grace_seconds: float = 10.0) -> None:
    """Exit cleanly when possible, abruptly only when a worker is wedged.

    A Playwright worker can block inside its own event loop with no timeout of
    its own. The pipeline records such a company as a Timeout failure and moves
    on, but the thread survives - and ThreadPoolExecutor threads are
    non-daemon, so the interpreter refuses to exit while one lingers.

    The old behaviour was to call ``os._exit`` unconditionally, which skips
    every ``atexit`` hook and buffered flush on *every* run to defend against a
    case that only sometimes happens. Instead, give the stragglers a short
    grace period: a normal run exits normally, and only a genuinely stuck
    worker gets the abrupt treatment.
    """
    sys.stdout.flush()
    sys.stderr.flush()

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        lingering = [
            t for t in threading.enumerate()
            if t is not threading.current_thread() and not t.daemon and t.is_alive()
        ]
        if not lingering:
            logging.shutdown()
            sys.exit(code)
        time.sleep(0.25)

    names = ", ".join(
        t.name for t in threading.enumerate()
        if t is not threading.current_thread() and not t.daemon and t.is_alive()
    )
    log.warning("Worker thread(s) still running after %.0fs (%s); exiting abruptly. "
                "All outputs are already written.", grace_seconds, names)
    logging.shutdown()
    os._exit(code)


if __name__ == "__main__":
    _exit(main())
