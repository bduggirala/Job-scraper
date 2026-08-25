"""Find a working ATS URL or job-search page for a company.

Claude-chat-style discovery replaced the missing URLs in config/companies.xlsx
by searching the web and using judgment. Code cannot judge, so this module
substitutes something stricter: it never decides whether a page *looks* like
the right careers site, it drives every candidate through a real collector and
keeps only what actually returns jobs.

Anything it cannot prove is reported as NOT_FOUND for the user to resolve by
hand. A guess written into the workbook is worse than a blank cell - the
hand-curated pass put marketing pages such as infosys.com/careers/ into the
ATS URL column, which routes to a collector that cannot parse them.

Not part of the pipeline: this is an on-demand tool (tools/find_ats_urls.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from ats.detector import (
    RADANCY,
    SUPPORTED_PROVIDERS,
    UNKNOWN,
    detect_ats,
    detect_from_html,
    extract_any_embedded_ats_url,
)
from ats.base import CollectionResult
from ats.html_utils import make_soup
from ats.router import COLLECTORS
import http_client
from logger import get_logger

log = get_logger("ats.discovery")

#: Written into the workbook when nothing could be verified. A first-class
#: outcome, not an error - the user resolves these manually.
NOT_FOUND = "NOT FOUND"


@dataclass
class Discovery:
    """What was proven about one company. Defaults mean "nothing found"."""

    company: str
    ats_url: str | None = None
    provider: str | None = None
    jobs_page: str | None = None
    jobs_found: int = 0
    method: str = "none"          # "http" | "browser" | "none"
    note: str = ""


def verify_ats_url(company: str, url: str, provider: str | None = None) -> tuple[int, str]:
    """Drive ``url`` through its real collector.

    Returns ``(jobs_found, note)``. A return of ``0`` means rejected - the
    caller must not write the URL anywhere. This is the only thing that
    promotes a candidate into a result.

    ``provider`` overrides URL-based detection for platforms that live on the
    company's own domain and so cannot be recognised from the URL alone
    (Radancy TalentBrew). The collector still receives the branded URL and
    derives its API host from it.
    """
    detection = detect_ats(url)
    if provider:
        detection = dict(detection)
        detection["provider"] = provider
        if not detection.get("host"):
            detection["host"] = (urlsplit(url).netloc or None)
    provider = detection.get("provider", UNKNOWN)

    collector_class = COLLECTORS.get(provider)
    if collector_class is None:
        return 0, f"no collector for provider {provider!r}"

    try:
        # coerce(): collectors return either a CollectionResult or (until they
        # are migrated) a bare list. Verification only cares about the count.
        collected = CollectionResult.coerce(collector_class(company, detection).collect())
    except Exception as exc:
        return 0, f"{provider} collector failed: {exc}"

    count = len(collected.jobs)
    if count == 0:
        return 0, f"{provider} collector returned zero jobs"
    if not collected.complete:
        return count, (
            f"{provider} API returned {count} jobs "
            f"(INCOMPLETE: {collected.stop_reason})"
        )
    return count, f"{provider} API returned {count} jobs"


def root_domain_url(url: str) -> str | None:
    """Corporate homepage for a careers URL.

    ``https://careers.frostbank.com/us/en`` -> ``https://frostbank.com``. A
    marketing careers page often does not link to the ATS while the corporate
    footer does, so both are worth crawling.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url if "//" in url else f"https://{url}")
    except ValueError:
        return None

    host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
    if not host or "." not in host or " " in host:
        return None

    labels = host.split(".")
    # Keep the last two labels for common TLDs, three for co.uk-style ones.
    if len(labels) > 2 and labels[-2] in {"co", "com", "org", "net", "gov", "ac"}:
        root = ".".join(labels[-3:])
    else:
        root = ".".join(labels[-2:])
    return f"https://{root}"


def candidates_from_html(html: str, base_url: str) -> list[str]:
    """ATS URLs a page reveals, most trustworthy first.

    Returns absolute URLs only. Judgment about whether any of them *work* is
    deliberately not made here - that is verify_ats_url's job.
    """
    if not html:
        return []

    found: list[str] = []

    provider = detect_from_html(html, final_url=base_url)
    providers = [provider] if provider in SUPPORTED_PROVIDERS else list(SUPPORTED_PROVIDERS)

    for candidate_provider in providers:
        embedded = extract_any_embedded_ats_url(html, candidate_provider)
        if embedded and embedded not in found:
            found.append(embedded)

    return found


def careers_links(html: str, base_url: str, limit: int = 5) -> list[str]:
    """Links on a page that plausibly lead to a job list.

    Reuses the same href hints the browser traversal ranks by, so the HTTP
    stage follows the same trail the browser would.
    """
    if not html:
        return []

    # Imported lazily: export_ats_urls.py imports this module and pipeline.py
    # imports that at module scope, so a top-level Playwright import here would
    # make every pipeline run load Playwright at startup - which the codebase
    # deliberately avoids so a machine without Chromium can still run the API
    # path (see ats/router.py::collect_via_browser).
    from browser.playwright_scraper import JOBS_PAGE_HREF_HINTS

    soup = make_soup(html)
    links: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if not any(hint in href.lower() for hint in JOBS_PAGE_HREF_HINTS):
            continue
        absolute = urljoin(base_url, href)
        if absolute not in links:
            links.append(absolute)
        if len(links) >= limit:
            break

    return links


#: Pages fetched per company in the HTTP stage: two seeds plus their
#: careers-ish links. Small on purpose - this stage is the cheap filter, the
#: browser stage is the thorough one.
MAX_HTTP_PAGES = 8


def _fetch(url: str) -> str:
    """One HTTP GET returning body text. Separated so tests can stub it."""
    return http_client.get_text(url, headers={"Accept": "text/html"})


def _seeds(seed_url: str | None) -> list[str]:
    seeds: list[str] = []
    if seed_url:
        seeds.append(seed_url)
        root = root_domain_url(seed_url)
        if root and root not in seeds:
            seeds.append(root)
    return seeds


def discover(company: str, seed_url: str | None, *, use_browser: bool = True) -> Discovery:
    """Find and verify an ATS URL or job-search page for one company.

    Never raises: every failure becomes a Discovery with ``method="none"``
    and the reason in ``note``, so one bad company cannot stop a sweep.
    """
    result = Discovery(company=company)

    seeds = _seeds(seed_url)
    if not seeds:
        result.note = "no seed URL in the workbook"
        return result

    visited: set[str] = set()
    queue = list(seeds)
    last_note = "nothing found in page HTML"

    while queue and len(visited) < MAX_HTTP_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            html = _fetch(url)
        except Exception as exc:
            last_note = f"fetch failed for {url}: {exc}"
            continue

        try:
            # Radancy TalentBrew lives on the company's own domain, so no ATS
            # host can be extracted from the HTML - the page URL itself is the
            # candidate, recognised only by fingerprint.
            if detect_from_html(html, final_url=url) == RADANCY:
                jobs_found, note = verify_ats_url(company, url, provider=RADANCY)
                if jobs_found:
                    result.ats_url = url
                    result.provider = RADANCY
                    result.jobs_found = jobs_found
                    result.method = "http"
                    result.note = note
                    return result
                last_note = note

            for candidate in candidates_from_html(html, url):
                jobs_found, note = verify_ats_url(company, candidate)
                if jobs_found:
                    result.ats_url = candidate
                    result.provider = detect_ats(candidate).get("provider")
                    result.jobs_found = jobs_found
                    result.method = "http"
                    result.note = note
                    return result
                last_note = note

            for link in careers_links(html, url):
                if link not in visited:
                    queue.append(link)
        except Exception as exc:
            # A malformed page or a throwing collector must cost this one URL,
            # not the sweep.
            last_note = f"page processing failed for {url}: {exc}"
            continue

    if use_browser:
        browser_result = _discover_via_browser(company, seeds[0])
        if browser_result.jobs_found:
            return browser_result
        last_note = browser_result.note or last_note

    result.note = last_note
    return result


def _try_radancy_from_rendered(company: str, page) -> Discovery | None:
    """Recognise Radancy TalentBrew from a rendered page and verify it.

    Returns a populated :class:`Discovery` when the collector proves jobs, else
    ``None`` so the browser stage keeps looking. The collector derives its API
    host from ``page.url``, so any page on the careers host works.
    """
    try:
        html = page.content()
    except Exception:
        return None
    if detect_from_html(html, final_url=page.url) != RADANCY:
        return None

    jobs_found, note = verify_ats_url(company, page.url, provider=RADANCY)
    if not jobs_found:
        return None

    return Discovery(
        company=company, ats_url=page.url, provider=RADANCY,
        jobs_found=jobs_found, method="browser", note=note,
    )


def _discover_via_browser(company: str, seed_url: str) -> Discovery:
    """Render the seed and crawl it, verifying whatever the traversal finds.

    Reuses the pipeline's own traversal rather than reimplementing it. The hop
    budget is raised because this tool is off the critical path - a normal run
    must stay inside its 360s per-company limit, a discovery sweep need not.
    """
    from browser.playwright_scraper import (
        _dismiss_cookie_banner,
        _extract_job_rows,
        _get_browser,
        _navigate_to_job_list,
        shutdown_thread_browser,
    )
    from settings import load_settings

    result = Discovery(company=company)
    cfg = load_settings()
    good_enough = int(cfg.get("playwright.hop_good_enough_rows", 10))
    timeout_ms = int(cfg.get("playwright.timeout_ms", 30000))
    user_agent = cfg.get("playwright.user_agent") or cfg.get("requests.user_agent")

    context = page = None
    try:
        browser = _get_browser()
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=user_agent,
            ignore_https_errors=True,
            locale="en-US",
        )
        context.set_default_timeout(timeout_ms)
        page = context.new_page()

        # Some enterprise careers hosts answer intermittent 5xx to automated
        # traffic (7-Eleven's homepage returns sporadic 504s). A single bad
        # render must not be recorded as NOT FOUND, so retry a transient
        # gateway error before giving up.
        settle_ms = int(cfg.get("playwright.wait_after_load_ms", 2500))
        for attempt in range(3):
            response = page.goto(seed_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)
            status = response.status if response is not None else 0
            if status < 500:
                break
            log.debug("%s: seed render got HTTP %s (attempt %s); retrying",
                      company, status, attempt + 1)
            page.wait_for_timeout(1500)
        _dismiss_cookie_banner(page)

        # Radancy TalentBrew renders on the company's own domain, so a plain
        # GET (which 504s or is bot-gated on some seeds, e.g. 7-Eleven's
        # homepage) never sees it - but the browser does. Fingerprint the
        # rendered page and hand off to the real collector, which drives the
        # results endpoint directly rather than scraping the DOM.
        radancy = _try_radancy_from_rendered(company, page)
        if radancy:
            return radancy

        traversal = _navigate_to_job_list(company, page, timeout_ms, max_hops=6)

        # The traversal may have hopped onto the Radancy-powered search page
        # even when the seed itself did not carry the fingerprint.
        radancy = _try_radancy_from_rendered(company, page)
        if radancy:
            return radancy

        if traversal.discovered_ats_url:
            jobs_found, note = verify_ats_url(company, traversal.discovered_ats_url)
            if jobs_found:
                result.ats_url = traversal.discovered_ats_url
                result.provider = traversal.discovered_provider
                result.jobs_found = jobs_found
                result.method = "browser"
                result.note = note
                return result
            result.note = note

        # No usable ATS, but the page the traversal landed on may itself be a
        # real job list. Only a substantial list counts: a landing page's
        # three featured roles is not a job search page.
        rows = traversal.jobs or _extract_job_rows(page)
        if len(rows) >= good_enough:
            result.jobs_page = page.url
            result.jobs_found = len(rows)
            result.method = "browser"
            result.note = f"rendered job list with {len(rows)} rows"
            return result

        result.note = result.note or f"browser found only {len(rows)} row(s)"
        return result

    except Exception as exc:
        result.note = f"browser stage failed: {exc}"
        return result
    finally:
        for closeable in (page, context):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:
                pass
        try:
            shutdown_thread_browser()
        except Exception:
            pass
