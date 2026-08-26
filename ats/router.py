"""Routing layer: company -> ATS detection -> collector or Playwright.

Routing is split into two phases so the run can be planned before any
scraping happens (this is what ``--dry-run`` prints, and what lets the
executor send API companies and browser companies to separate thread pools):

    plan_route(...)     -> decide provider + method (cheap, or one GET when
                           page resolution is enabled)
    fetch_company_jobs(...) -> execute the plan and return normalized records
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ats.amazon import AmazonJobsCollector
from ats.ashby import AshbyCollector
from ats.avature import AvatureCollector
from ats.base import (
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
    SCRAPING_METHOD_BROWSER,
)
from ats.cornerstone import CornerstoneCollector
from ats.detector import UNKNOWN, detect_ats
from ats.eightfold import EightfoldCollector
from ats.framework_data import FrameworkDataCollector
from ats.greenhouse import GreenhouseCollector
from ats.icims import ICIMSCollector
from ats.jibe import JibeCollector
from ats.jobvite import JobviteCollector
from ats.jsonld import JSONLDCollector
from ats.lever import LeverCollector
from ats.paylocity import PaylocityCollector
from ats.phenom import PhenomCollector
from ats.radancy import RadancyCollector
from ats.resolver import resolve_from_page
from ats.smartrecruiters import SmartRecruitersCollector
from ats.static_html import StaticHTMLCollector
from ats.url_repair import repair_careers_url
from ats.successfactors import SuccessFactorsCollector
from ats.taleo import TaleoCollector
from ats.ukg import UKGCollector
from ats.workday import WorkdayCollector
from logger import get_logger
from normalize import build_record

log = get_logger("ats.router")

#: provider name -> collector class. Membership here is what "supported
#: direct collector" means for routing decisions.
COLLECTORS: dict[str, type[ATSCollector]] = {
    "workday": WorkdayCollector,
    "greenhouse": GreenhouseCollector,
    "lever": LeverCollector,
    "ashby": AshbyCollector,
    "smartrecruiters": SmartRecruitersCollector,
    "paylocity": PaylocityCollector,
    "ukg": UKGCollector,
    "taleo": TaleoCollector,
    "icims": ICIMSCollector,
    "phenom": PhenomCollector,
    "successfactors": SuccessFactorsCollector,
    "avature": AvatureCollector,
    "eightfold": EightfoldCollector,
    "radancy": RadancyCollector,
    "amazon": AmazonJobsCollector,
    "jobvite": JobviteCollector,
    "cornerstone": CornerstoneCollector,
    "jibe": JibeCollector,
}

METHOD_API = "direct_api"
METHOD_BROWSER = SCRAPING_METHOD_BROWSER

SOURCE_ATS_URL = "ats_url"
SOURCE_LIVE_PAGE = "live_jobs_page"


@dataclass
class RoutePlan:
    """The decision for one company, before any jobs are fetched."""

    company: str
    url: str | None
    provider: str
    method: str
    source: str
    detection: dict[str, Any] = field(default_factory=dict)
    resolved_via_page: bool = False
    note: str | None = None
    #: The branded careers page a human would actually visit, captured before
    #: page resolution may overwrite ``url`` with a discovered ATS link. Some
    #: discovered links are wrong (e.g. a "My Submissions" login link on a
    #: Taleo tenant, picked up because it merely matches the provider's URL
    #: pattern) - the browser fallback should render the real page rather
    #: than repeat the same bad guess the API attempt already failed on.
    original_url: str | None = None
    #: The exact value read from the workbook column named by ``source``,
    #: before :func:`ats.url_repair.repair_careers_url` may have replaced a
    #: dead hostname with a live one. ``None`` when no repair ran.
    raw_url: str | None = None
    #: True when ``url`` differs from ``raw_url`` because it was repaired
    #: this run - lets the pipeline write the verified live URL back over
    #: the dead one it replaced (see ``pipeline.py``'s write-back step).
    was_repaired: bool = False
    #: The workbook's ``Live Jobs Page`` value, kept even when the ``ATS URL``
    #: column won the routing decision. A tenant retired by an acquisition or
    #: an ATS migration leaves a dead ATS URL next to a careers page that still
    #: works, and re-rendering the dead URL in the browser finds nothing -
    #: confirmed against McAfee (Workday answers total:0) and HCLTech.
    live_jobs_url: str | None = None

    @property
    def uses_browser(self) -> bool:
        return self.method == METHOD_BROWSER

    def describe(self) -> str:
        provider = self.provider if self.provider != UNKNOWN else "Unknown"
        method = "Direct API" if self.method == METHOD_API else "Playwright"
        suffix = " (resolved from page)" if self.resolved_via_page else ""
        return f"{self.company} -> {provider.title()} -> {method}{suffix}"


@dataclass
class CompanyResult:
    """Outcome of scraping one company."""

    company: str
    jobs: list[dict]
    plan: RoutePlan
    success: bool
    error_type: str | None = None
    error_message: str | None = None
    fell_back: bool = False
    #: True only when the collector is confident it saw every job the provider
    #: would serve. ``pipeline.run()`` gates removal sync on this, never on
    #: ``success`` - a partial harvest is a real success that must not be read
    #: as "the jobs we didn't reach have closed". Defaults True so paths that
    #: never paginate (JSON-LD, browser) keep today's behaviour.
    complete: bool = True
    #: Why collection stopped, when it stopped short. See ``ats.base``.
    stop_reason: str | None = None
    #: What the provider claimed it had, when it said.
    reported_total: int | None = None
    discovered_ats_url: str | None = None
    discovered_provider: str | None = None
    #: True only when the discovered URL was actually driven through its
    #: collector successfully during this run. The pipeline writes back only
    #: verified discoveries, so a URL that merely *looks* like an ATS never
    #: lands in the workbook.
    discovery_verified: bool = False


def _good_enough_rows() -> int:
    """Row count that counts as a genuine job list rather than featured roles.

    Shared with the browser traversal (``playwright.hop_good_enough_rows``) on
    purpose: "is this a real list?" is the same question wherever it is asked,
    and the JSON-LD tier previously answered it with "any row at all".
    """
    from settings import load_settings
    return int(load_settings().get("playwright.hop_good_enough_rows", 10))


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    text = str(value).strip().lower()
    return text in {"", "nan", "none", "n/a", "na", "-", "null"}


def plan_route(
    company: str,
    ats_url: str | None = None,
    live_jobs_url: str | None = None,
    *,
    resolve_pages: bool = True,
    playwright_enabled: bool = True,
) -> RoutePlan:
    """Decide how a company should be scraped.

    Follows the specified logic: prefer the ATS URL, fall back to the live
    jobs page, and use Playwright only when no supported direct collector can
    be identified.
    """
    live_page = None if _blank(live_jobs_url) else str(live_jobs_url).strip()

    if not _blank(ats_url):
        url, source = str(ats_url).strip(), SOURCE_ATS_URL
    elif live_page:
        url, source = live_page, SOURCE_LIVE_PAGE
    else:
        return RoutePlan(
            company=company, url=None, provider=UNKNOWN, method=METHOD_BROWSER,
            source=SOURCE_ATS_URL, note="No URL provided for this company",
        )

    raw_url = url
    was_repaired = False

    # Several workbook URLs point at retired careers.* subdomains that no
    # longer resolve. Swap in a live equivalent before doing anything else,
    # so a stale hostname is not recorded as a permanent failure.
    if resolve_pages:
        repaired = repair_careers_url(company, url)
        if repaired:
            url = repaired
            was_repaired = True

    # Captured before page resolution can overwrite ``url`` below - this is
    # the page a browser fallback should land on, not whatever ATS link the
    # resolver guesses.
    original_url = url

    detection = detect_ats(url)
    provider = detection["provider"]

    # Direct lexical hit on a supported provider.
    if provider in COLLECTORS:
        return RoutePlan(
            company=company, url=url, provider=provider, method=METHOD_API,
            source=source, detection=detection, original_url=original_url,
            raw_url=raw_url, was_repaired=was_repaired, live_jobs_url=live_page,
        )

    # Unknown from the URL alone: probe the page for an embedded/redirected ATS.
    if resolve_pages:
        resolved = resolve_from_page(company, url)
        if resolved["provider"] in COLLECTORS:
            return RoutePlan(
                company=company, url=resolved.get("url") or url,
                provider=resolved["provider"], method=METHOD_API, source=source,
                detection=resolved, resolved_via_page=True, original_url=original_url,
                raw_url=raw_url, was_repaired=was_repaired, live_jobs_url=live_page,
            )

    method = METHOD_BROWSER if playwright_enabled else METHOD_API
    note = None if playwright_enabled else "Playwright disabled; no direct collector available"
    return RoutePlan(
        company=company, url=url, provider=UNKNOWN, method=method,
        source=source, detection=detection, note=note, original_url=original_url,
        raw_url=raw_url, was_repaired=was_repaired, live_jobs_url=live_page,
    )


def collect_via_api(plan: RoutePlan) -> CollectionResult:
    """Run the direct collector for a planned company.

    Always returns a :class:`~ats.base.CollectionResult`, even for collectors
    that still return a bare list - :meth:`CollectionResult.coerce` wraps those
    as complete, preserving their current behaviour until they are converted.

    Raises:
        CollectorUnavailable: the API could not serve this tenant.
    """
    collector_class = COLLECTORS.get(plan.provider)
    if collector_class is None:
        raise CollectorUnavailable(f"No collector registered for provider {plan.provider!r}")

    detection = dict(plan.detection or {})
    detection.setdefault("url", plan.url)
    collector = collector_class(plan.company, detection)
    return CollectionResult.coerce(collector.collect())


def collect_via_jsonld(plan: RoutePlan) -> list[dict]:
    """Try harvesting schema.org JobPosting JSON-LD from the planned page.

    A generic, provider-agnostic tier that sits between page resolution and the
    Playwright fallback: many career pages embed JobPosting structured data for
    SEO, which we can read over a single HTTP GET instead of driving a browser.

    Raises:
        CollectorUnavailable: the page carries no JobPosting JSON-LD (or the
            fetch failed), signalling the router to escalate to Playwright.
    """
    detection = dict(plan.detection or {})
    detection.setdefault("url", plan.url)
    return JSONLDCollector(plan.company, detection).collect()


def collect_via_static_html(plan: RoutePlan) -> list[dict]:
    """Harvest a server-rendered job list over one GET.

    Raises:
        CollectorUnavailable: the page carries no job-shaped links.
    """
    detection = dict(plan.detection or {})
    detection.setdefault("url", plan.url)
    return StaticHTMLCollector(plan.company, detection).collect().jobs


def collect_via_framework_data(plan: RoutePlan) -> list[dict]:
    """Read a Next.js/Nuxt/Redux hydration payload instead of rendering it.

    Raises:
        CollectorUnavailable: no framework payload, or none carrying jobs.
    """
    detection = dict(plan.detection or {})
    detection.setdefault("url", plan.url)
    return FrameworkDataCollector(plan.company, detection).collect().jobs


def collect_via_browser(plan: RoutePlan) -> tuple[list[dict], str | None, str | None, bool]:
    """Run the Playwright fallback for a planned company.

    Imported lazily so that a run which never needs a browser (or a machine
    without Chromium installed) does not pay the import cost or fail at start.

    Returns:
        ``(records, discovered_ats_url, discovered_provider)`` - the latter two
        are set only when the search fallback's network sniffing recognized a
        real ATS behind an otherwise-custom career page.
    """
    from browser.playwright_scraper import scrape_with_playwright

    # When the plan's URL came from resolver-guessed ATS discovery (not the
    # workbook's own ATS URL), that guess may be wrong - e.g. a Taleo tenant's
    # "My Submissions" login link matches the provider's URL pattern but has
    # no job search on it. The direct-API attempt on that guess already
    # failed to get here; render the real branded page instead of repeating
    # the same bad guess in the browser.
    browser_url = plan.url
    if plan.resolved_via_page and plan.original_url:
        browser_url = plan.original_url

    if not browser_url:
        raise CollectorUnavailable("No URL to open in the browser")

    result = scrape_with_playwright(plan.company, browser_url)

    records = []
    for job in result.jobs:
        record = build_record(
            company=plan.company,
            title=job.get("title"),
            location=job.get("location"),
            date_posted=job.get("date_posted"),
            job_url=job.get("job_url"),
            employment_type=job.get("employment_type"),
            description=job.get("description"),
            ats_provider=plan.provider if plan.provider != UNKNOWN else "unknown",
            scraping_method=METHOD_BROWSER,
        )
        if record:
            # Provenance: which search term surfaced this row, when the site
            # only reveals jobs behind a search.
            record["source_query"] = job.get("source_query")
            records.append(record)
    return (
        records,
        result.discovered_ats_url,
        result.discovered_provider,
        result.blocked,
    )


def fetch_company_jobs(
    company: str,
    ats_url: str | None = None,
    live_jobs_url: str | None = None,
    *,
    plan: RoutePlan | None = None,
    resolve_pages: bool = True,
    playwright_enabled: bool = True,
) -> CompanyResult:
    """Scrape one company end to end.

    Never raises: every failure is captured on the returned
    :class:`CompanyResult` so a single bad company cannot stop the run.
    """
    if plan is None:
        plan = plan_route(
            company, ats_url, live_jobs_url,
            resolve_pages=resolve_pages, playwright_enabled=playwright_enabled,
        )

    if plan.url is None:
        return CompanyResult(
            company=company, jobs=[], plan=plan, success=False,
            error_type="NoURL", error_message=plan.note or "No URL configured",
        )

    fell_back = False

    if plan.method == METHOD_API:
        log.info("%s -> %s", company, plan.provider.title())
        try:
            collected = collect_via_api(plan)
            jobs = collected.jobs
            log.info("%s -> %s jobs retrieved%s", company, len(jobs),
                     "" if collected.complete
                     else f" (INCOMPLETE: {collected.stop_reason})")
            # A page-resolved provider (plan.url came from resolve_from_page,
            # not straight from the workbook) is just as verified as a
            # browser-discovered one once it has actually returned jobs -
            # write it back the same way so the next run skips resolution.
            if plan.resolved_via_page:
                return CompanyResult(
                    company=company, jobs=jobs, plan=plan, success=True,
                    discovered_ats_url=plan.url, discovered_provider=plan.provider,
                    discovery_verified=True,
                    complete=collected.complete, stop_reason=collected.stop_reason,
                    reported_total=collected.reported_total,
                )
            return CompanyResult(
                company=company, jobs=jobs, plan=plan, success=True,
                complete=collected.complete, stop_reason=collected.stop_reason,
                reported_total=collected.reported_total,
            )
        except CollectorUnavailable as exc:
            if not playwright_enabled:
                return CompanyResult(
                    company=company, jobs=[], plan=plan, success=False,
                    error_type="CollectorUnavailable", error_message=str(exc),
                )
            log.warning("%s -> %s API unavailable (%s); falling back to Playwright",
                        company, plan.provider, exc)
            plan.method = METHOD_BROWSER
            plan.note = f"direct API unavailable: {exc}"
            fell_back = True

            # A collector that failed on the workbook's ATS URL usually means
            # that tenant is gone - retired by an acquisition, a rebrand or an
            # ATS migration. Re-rendering the same dead URL finds nothing,
            # while the Live Jobs Page column often holds a careers site that
            # still works and has never been tried. Confirmed against McAfee,
            # whose Workday tenant answers total:0 beside a live careers page,
            # and HCLTech.
            if (plan.source == SOURCE_ATS_URL and plan.live_jobs_url
                    and plan.live_jobs_url != plan.url):
                log.info("%s: ATS URL is not serving; switching the browser to "
                         "the Live Jobs Page (%s)", company, plan.live_jobs_url[:90])
                plan.url = plan.live_jobs_url
                plan.original_url = plan.live_jobs_url
                plan.source = SOURCE_LIVE_PAGE
        except Exception as exc:
            return CompanyResult(
                company=company, jobs=[], plan=plan, success=False,
                error_type=type(exc).__name__, error_message=str(exc),
            )

    # JSON-LD tier: harvest schema.org JobPosting structured data over a single
    # HTTP GET before paying for a browser. Runs for an unrecognised provider
    # *and* for a known provider whose collector just failed - that is the case
    # where a cheap tier is most valuable, and it used to be skipped.
    # The cheap tiers, in ascending cost: structured data, then a
    # server-rendered list, then a framework hydration payload. All three are
    # one GET and provider-agnostic, and every company they answer is a
    # company that never has to pay for a Chromium instance.
    #
    # The "is this a real job list?" floor is applied here, once, for all of
    # them - a landing page routinely embeds two or three featured roles for
    # SEO, and accepting those reports 3 jobs for an employer with thousands.
    # A thin harvest is kept as a fallback while the ladder continues.
    good_enough = _good_enough_rows()
    best_cheap: list[dict] = []

    for label, harvest in (
        ("JSON-LD", collect_via_jsonld),
        ("static HTML", collect_via_static_html),
        ("framework data", collect_via_framework_data),
    ):
        if not plan.url:
            break
        try:
            rows = harvest(plan)
        except CollectorUnavailable:
            continue
        except Exception as exc:  # a parser hiccup must not sink the company
            log.debug("%s -> %s tier errored (%s)", company, label, exc)
            continue

        if len(rows) >= good_enough:
            log.info("%s -> %s jobs via %s", company, len(rows), label)
            return CompanyResult(
                company=company, jobs=rows, plan=plan, success=True,
                fell_back=fell_back,
            )
        if len(rows) > len(best_cheap):
            log.debug("%s -> %s found only %s row(s); keeping as a fallback",
                      company, label, len(rows))
            best_cheap = rows

    jsonld_jobs = best_cheap

    log.info("%s -> Playwright fallback", company)
    try:
        jobs, discovered_url, discovered_provider, blocked = collect_via_browser(plan)
    except Exception as exc:
        # A thin JSON-LD harvest is still better than nothing when the browser
        # cannot run at all.
        if jsonld_jobs:
            log.info("%s -> browser failed (%s); keeping %s JSON-LD row(s)",
                     company, exc, len(jsonld_jobs))
            return CompanyResult(
                company=company, jobs=jsonld_jobs, plan=plan, success=True,
                fell_back=fell_back,
            )
        return CompanyResult(
            company=company, jobs=[], plan=plan, success=False, fell_back=fell_back,
            error_type=type(exc).__name__, error_message=str(exc),
        )

    # Neither tier found a real list: keep whichever saw more.
    if len(jsonld_jobs) > len(jobs) and not discovered_provider:
        log.info("%s -> keeping %s JSON-LD row(s) over %s browser row(s)",
                 company, len(jsonld_jobs), len(jobs))
        jobs = jsonld_jobs

    # Self-healing: the browser found the real ATS behind a branded careers
    # page (e.g. GameStop -> UKG). Collecting through that provider's API now
    # is strictly better than whatever HTML scraping produced, so retry via
    # the collector immediately rather than waiting for the next run to pick
    # up the written-back URL.
    if discovered_provider in COLLECTORS and discovered_url:
        healed = RoutePlan(
            company=company, url=discovered_url, provider=discovered_provider,
            method=METHOD_API, source=plan.source,
            detection=detect_ats(discovered_url), resolved_via_page=True,
            note="discovered via browser during this run",
        )
        try:
            healed_collected = collect_via_api(healed)
            log.info("%s -> %s jobs retrieved via discovered %s API",
                     company, len(healed_collected.jobs), discovered_provider)
            return CompanyResult(
                company=company, jobs=healed_collected.jobs, plan=healed, success=True,
                fell_back=fell_back,
                discovered_ats_url=discovered_url,
                discovered_provider=discovered_provider,
                discovery_verified=True,
                complete=healed_collected.complete,
                stop_reason=healed_collected.stop_reason,
                reported_total=healed_collected.reported_total,
            )
        except Exception as exc:
            log.warning("%s -> discovered %s API failed (%s); keeping browser rows",
                        company, discovered_provider, exc)

    log.info("%s -> %s jobs retrieved", company, len(jobs))

    # A refusal is reported as itself rather than folded into "no jobs found",
    # so the failure report distinguishes "the site turned us away" from
    # "we reached the site and it had nothing" - which need opposite responses.
    if blocked and not jobs:
        return CompanyResult(
            company=company, jobs=[], plan=plan, success=False, fell_back=fell_back,
            error_type="AccessDenied",
            error_message="Site answered with a bot challenge or explicit denial",
        )

    # Reaching a site that has no matching openings is a real answer, not a
    # failure. Treating it as one inflated the failure count, filled the
    # failure report with rows needing no action, and wrote a misleading
    # "Data Retrieved = FALSE" into the workbook.
    #
    # This is safe for removal because sync_completed_companies also requires
    # result.jobs - a zero-job result is still never read as "all jobs closed".
    if not jobs:
        log.info("%s -> rendered cleanly with no matching jobs", company)

    return CompanyResult(
        company=company, jobs=jobs, plan=plan,
        success=True, fell_back=fell_back,
        discovered_ats_url=discovered_url, discovered_provider=discovered_provider,
    )
