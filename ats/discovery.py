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
    SUPPORTED_PROVIDERS,
    UNKNOWN,
    detect_ats,
    detect_from_html,
    extract_any_embedded_ats_url,
)
from ats.html_utils import make_soup
from ats.router import COLLECTORS
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


def verify_ats_url(company: str, url: str) -> tuple[int, str]:
    """Drive ``url`` through its real collector.

    Returns ``(jobs_found, note)``. A return of ``0`` means rejected - the
    caller must not write the URL anywhere. This is the only thing that
    promotes a candidate into a result.
    """
    detection = detect_ats(url)
    provider = detection.get("provider", UNKNOWN)

    collector_class = COLLECTORS.get(provider)
    if collector_class is None:
        return 0, f"no collector for provider {provider!r}"

    try:
        jobs = collector_class(company, detection).collect()
    except Exception as exc:
        return 0, f"{provider} collector failed: {exc}"

    count = len(jobs or [])
    if count == 0:
        return 0, f"{provider} collector returned zero jobs"
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
