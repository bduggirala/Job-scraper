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
from ats.base import ATSCollector, CollectorUnavailable, SCRAPING_METHOD_BROWSER
from ats.cornerstone import CornerstoneCollector
from ats.detector import UNKNOWN, detect_ats
from ats.eightfold import EightfoldCollector
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
    discovered_ats_url: str | None = None
    discovered_provider: str | None = None
    #: True only when the discovered URL was actually driven through its
    #: collector successfully during this run. The pipeline writes back only
    #: verified discoveries, so a URL that merely *looks* like an ATS never
    #: lands in the workbook.
    discovery_verified: bool = False


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
    if not _blank(ats_url):
        url, source = str(ats_url).strip(), SOURCE_ATS_URL
    elif not _blank(live_jobs_url):
        url, source = str(live_jobs_url).strip(), SOURCE_LIVE_PAGE
    else:
        return RoutePlan(
            company=company, url=None, provider=UNKNOWN, method=METHOD_BROWSER,
            source=SOURCE_ATS_URL, note="No URL provided for this company",
        )

    # Several workbook URLs point at retired careers.* subdomains that no
    # longer resolve. Swap in a live equivalent before doing anything else,
    # so a stale hostname is not recorded as a permanent failure.
    if resolve_pages:
        repaired = repair_careers_url(company, url)
        if repaired:
            url = repaired

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
        )

    # Unknown from the URL alone: probe the page for an embedded/redirected ATS.
    if resolve_pages:
        resolved = resolve_from_page(company, url)
        if resolved["provider"] in COLLECTORS:
            return RoutePlan(
                company=company, url=resolved.get("url") or url,
                provider=resolved["provider"], method=METHOD_API, source=source,
                detection=resolved, resolved_via_page=True, original_url=original_url,
            )

    method = METHOD_BROWSER if playwright_enabled else METHOD_API
    note = None if playwright_enabled else "Playwright disabled; no direct collector available"
    return RoutePlan(
        company=company, url=url, provider=UNKNOWN, method=method,
        source=source, detection=detection, note=note, original_url=original_url,
    )


def collect_via_api(plan: RoutePlan) -> list[dict]:
    """Run the direct collector for a planned company.

    Raises:
        CollectorUnavailable: the API could not serve this tenant.
    """
    collector_class = COLLECTORS.get(plan.provider)
    if collector_class is None:
        raise CollectorUnavailable(f"No collector registered for provider {plan.provider!r}")

    detection = dict(plan.detection or {})
    detection.setdefault("url", plan.url)
    collector = collector_class(plan.company, detection)
    return collector.collect()


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


def collect_via_browser(plan: RoutePlan) -> tuple[list[dict], str | None, str | None]:
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

    records = [
        build_record(
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
        for job in result.jobs
    ]
    return (
        [record for record in records if record],
        result.discovered_ats_url,
        result.discovered_provider,
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
            jobs = collect_via_api(plan)
            log.info("%s -> %s jobs retrieved", company, len(jobs))
            return CompanyResult(company=company, jobs=jobs, plan=plan, success=True)
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
        except Exception as exc:
            return CompanyResult(
                company=company, jobs=[], plan=plan, success=False,
                error_type=type(exc).__name__, error_message=str(exc),
            )

    # JSON-LD tier: for pages with no recognised provider, try harvesting
    # schema.org JobPosting structured data over HTTP before paying for a
    # browser. If the page carries none, CollectorUnavailable drops us through
    # to the Playwright fallback unchanged.
    if plan.provider == UNKNOWN and plan.url:
        try:
            jsonld_jobs = collect_via_jsonld(plan)
        except CollectorUnavailable:
            jsonld_jobs = []
        except Exception as exc:  # a parser hiccup must not sink the company
            log.debug("%s -> JSON-LD tier errored (%s)", company, exc)
            jsonld_jobs = []
        if jsonld_jobs:
            log.info("%s -> %s jobs via JSON-LD fallback", company, len(jsonld_jobs))
            return CompanyResult(
                company=company, jobs=jsonld_jobs, plan=plan, success=True,
                fell_back=fell_back,
            )

    log.info("%s -> Playwright fallback", company)
    try:
        jobs, discovered_url, discovered_provider = collect_via_browser(plan)
    except Exception as exc:
        return CompanyResult(
            company=company, jobs=[], plan=plan, success=False, fell_back=fell_back,
            error_type=type(exc).__name__, error_message=str(exc),
        )

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
            healed_jobs = collect_via_api(healed)
            log.info("%s -> %s jobs retrieved via discovered %s API",
                     company, len(healed_jobs), discovered_provider)
            return CompanyResult(
                company=company, jobs=healed_jobs, plan=healed, success=True,
                fell_back=fell_back,
                discovered_ats_url=discovered_url,
                discovered_provider=discovered_provider,
                discovery_verified=True,
            )
        except Exception as exc:
            log.warning("%s -> discovered %s API failed (%s); keeping browser rows",
                        company, discovered_provider, exc)

    log.info("%s -> %s jobs retrieved", company, len(jobs))
    return CompanyResult(
        company=company, jobs=jobs, plan=plan,
        success=bool(jobs), fell_back=fell_back,
        error_type=None if jobs else "NoJobsFound",
        error_message=None if jobs else "Browser fallback returned zero jobs",
        discovered_ats_url=discovered_url, discovered_provider=discovered_provider,
    )
