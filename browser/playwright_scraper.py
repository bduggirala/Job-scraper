"""Playwright fallback scraper for career sites with no usable API.

Used only when the router cannot identify a supported ATS, per the pipeline
rules. Design points that matter for reliability:

* **Thread safety.** Playwright's sync API is not shareable across threads, so
  each worker thread lazily creates its own Playwright driver + Chromium
  instance in thread-local storage. :func:`shutdown_browsers` tears them down.
* **Isolation.** Every company gets a fresh browser *context*, so cookies and
  storage never leak between companies, but the expensive browser process is
  reused across companies on the same thread.
* **Containment.** Every failure mode (navigation timeout, selector timeout,
  crashed page) is caught and converted into an empty result or a raised
  exception the router records - one bad company never stops the run.
* **Search fallback.** Some career pages render nothing until a keyword is
  typed into a search box (confirmed on Goldman Sachs, FedEx). When the first
  load yields zero jobs, this module types one configured search term and
  retries - and while it does, it watches network traffic for a real ATS
  endpoint the static-HTML resolver never saw, since those only fire on
  interaction. See :func:`scrape_with_playwright`.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from ats.detector import (
    ASHBY,
    EIGHTFOLD,
    GREENHOUSE,
    LEVER,
    RADANCY,
    SMARTRECRUITERS,
    UNKNOWN,
    WORKDAY,
    detect_ats,
    detect_from_html,
    extract_all_embedded_ats_urls,
)
from ats.html_utils import iter_jsonld_jobs, jsonld_location
from logger import get_logger
from settings import load_settings

log = get_logger("browser")

_thread_local = threading.local()
_all_instances: list[tuple[Any, Any, Any]] = []
_instances_lock = threading.Lock()

# Anchors that look like individual postings across most career sites.
JOB_LINK_SELECTORS = (
    'a[href*="/job/"]',
    'a[href*="/jobs/"]',
    'a[href*="jobId="]',
    'a[href*="jobid="]',
    'a[href*="/JobDetail"]',
    'a[href*="/jobdetail"]',
    'a[href*="/requisition"]',
    'a[href*="reqId="]',
    'a[href*="/posting/"]',
    'a[href*="/vacancy"]',
    'a[href*="/opening"]',
    'a[href*="/opportunity/"]',
    'a[href*="/position/"]',
    'a[href*="/careers/job"]',
    # Goldman Sachs' higher.gs.com lists every posting as /roles/{id}; the
    # diagnostic in tools/probe_site.py already treated /role/ as job-like,
    # but the scraper's selectors did not, so 1,119 matches extracted as zero.
    'a[href*="/roles/"]',
    'a[href*="/role/"]',
    'a[data-ph-at-id="job-link"]',
    'a[data-automation-id*="jobTitle"]',
    "a.job-title-link",
    "a.jobTitle",
    "a.job-title",
    "a.jobs-list-item__link",
    "a.linkForJob",
    '[class*="job-card"] a',
    '[class*="jobCard"] a',
    '[class*="job-result"] a',
    # Motion Recruitment's job list items carry no href keyword at all
    # (/tech-jobs/{city}/{type}/{slug}/{id}) - only the CSS-module container
    # class ("JobItem_module_jobItem") identifies them. Confirmed live: 20
    # of 20 cards on a page extracted as zero without this selector.
    '[class*="jobitem" i] a',
    '[class*="search-result"] a[href]',
)

# Links from a careers *landing* page through to the actual job list. Many
# corporate careers URLs in the workbook are marketing pages ("Life at X")
# whose openings live one hop away behind "Search jobs" / "View openings".
JOBS_PAGE_LINK_TEXT = (
    "search jobs", "search all jobs", "view all jobs", "see all jobs",
    "all jobs", "browse jobs", "find jobs", "job search", "search openings",
    "view openings", "open positions", "open roles", "current openings",
    "job openings", "view opportunities", "search opportunities",
    "explore jobs", "explore opportunities", "apply now", "we're hiring",
    "were hiring", "join our team", "view jobs", "search careers",
)

# Href fragments that mark a link as leading to a job list.
JOBS_PAGE_HREF_HINTS = (
    "/search", "/jobs", "/job-search", "/joblist", "/job-list",
    "/openings", "/opportunities", "/positions", "/vacancies",
    "/careers/search", "/all-jobs", "/jobsearch",
)

# Buttons that reveal more results without a URL change.
LOAD_MORE_SELECTORS = (
    'button:has-text("Load more")',
    'button:has-text("Show more")',
    'button:has-text("View more")',
    'button:has-text("More jobs")',
    'a:has-text("Load more")',
    'a:has-text("Show more")',
    '[data-ph-at-id="pagination-next-button"]',
    'button[aria-label*="next" i]',
    'a[aria-label*="next" i]',
)

# Common cookie/consent banner buttons that otherwise block clicks/typing.
COOKIE_CONSENT_SELECTORS = (
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Accept All Cookies")',
    'button:has-text("Allow all")',
    'button:has-text("Allow All")',
    'button:has-text("I Agree")',
    'button:has-text("I Accept")',
    'button:has-text("Got it")',
    'button[id*="accept" i]',
    'button[class*="accept" i]',
    '#onetrust-accept-btn-handler',
)

# Rotated on navigation retries - some sites reset the connection for a
# repeat visitor presenting an identical fingerprint.
RETRY_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",
)

RETRY_VIEWPORTS = (
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
)

# Search-input heuristics: placeholder/aria-label/name/id text that marks a
# box as a job-keyword search versus a location box (which must be excluded -
# FedEx has both side by side, and typing a role into the location box is
# wrong).
_SEARCH_INPUT_HINT_RE = re.compile(r"search|keyword|\btitle\b|role|position|what", re.I)
_LOCATION_INPUT_HINT_RE = re.compile(r"location|city|zip|postal|where|address", re.I)

_NAV_NOISE = {
    "apply", "apply now", "view job", "details", "job details", "learn more",
    "read more", "next", "previous", "back", "search", "save job", "share",
    "sign in", "login", "home", "view all jobs", "see all jobs", "more",
    "all jobs", "browse jobs", "job search",
}

# Call-to-action phrases that appear *inside* an anchor's text. The broadened
# job-link selectors otherwise promote site navigation into "jobs" - observed
# live: CBRE returned "Join Our Community", Globe Life "Search Jobs", Liberty
# Mutual "Current Employees" and L3Harris "HIRING EVENT" as their only
# results. A company reporting one junk row is worse than a clean failure,
# because it looks like success.
_NAV_PHRASES = (
    "join our", "join the", "search career", "search job", "search open",
    "view open", "view opportunit", "view job", "browse job", "explore job",
    "explore opportunit", "current employee", "hiring event", "job alert",
    "create profile", "create account", "sign up", "talent network",
    "talent community", "life at", "why work", "our culture", "benefits",
    "meet our", "learn about", "see open", "find your", "start your",
    "all locations", "all departments", "view all", "see all", "show all",
    "career website", "career site", "jobs by", "job openings", "saved job",
    "view more", "load more", "show more", "our jobs", "job categor",
    "search our", "back to", "go to", "read our", "follow us",
)


def _is_nav_text(title: str) -> bool:
    """True when anchor text reads as site navigation rather than a posting."""
    lowered = title.strip().lower()
    if lowered in _NAV_NOISE:
        return True
    if any(phrase in lowered for phrase in _NAV_PHRASES):
        return True
    # Short all-caps CTAs ("VIEW OPPORTUNITIES", "HIRING EVENT").
    if title.isupper() and len(title.split()) <= 3:
        return True
    return False

# Extracts (title, href, nearby-location) triples from the rendered DOM.
_EXTRACT_JS = """
(selectors) => {
  const out = [];
  const seen = new Set();
  const locationRe = /(location|city|region|job-location|jobLocation)/i;

  const nearbyLocation = (el) => {
    let node = el.parentElement;
    for (let depth = 0; depth < 3 && node; depth++) {
      const candidates = node.querySelectorAll('[class],[data-ph-at-id]');
      for (const cand of candidates) {
        const marker = (cand.className || '') + ' ' + (cand.getAttribute('data-ph-at-id') || '');
        if (locationRe.test(marker)) {
          const text = (cand.innerText || '').trim();
          if (text && text.length < 120) return text;
        }
      }
      node = node.parentElement;
    }
    return null;
  };

  const dateRe = /(date|posted|time|age)/i;

  const nearbyDate = (el) => {
    let node = el.parentElement;
    for (let depth = 0; depth < 3 && node; depth++) {
      // A <time datetime> attribute is the most reliable signal.
      const t = node.querySelector('time[datetime]');
      if (t) {
        const dt = t.getAttribute('datetime');
        if (dt) return dt;
      }
      for (const cand of node.querySelectorAll('[class],[data-ph-at-id]')) {
        const marker = (cand.className || '') + ' ' +
                       (cand.getAttribute('data-ph-at-id') || '');
        if (dateRe.test(marker)) {
          const text = (cand.innerText || '').trim();
          if (text && text.length < 60) return text;
        }
      }
      node = node.parentElement;
    }
    return null;
  };

  // Some cards (Pyramid Consulting's Sprockets.ai board) link only an
  // "Apply Now" button, with the actual title in a sibling <h3> elsewhere
  // in the card - the anchor's own text is never the title. Confirmed
  // live: all 72 cards on the page extracted as "Apply Now" x72 (and were
  // then correctly discarded as nav chrome), losing every job.
  const genericCtaRe = /^(apply( now)?|view( job| details)?|learn more|read more|see more|details)$/i;

  const nearbyHeading = (el) => {
    let node = el.parentElement;
    for (let depth = 0; depth < 4 && node; depth++) {
      const heading = node.querySelector('h1,h2,h3,h4,h5,h6');
      if (heading) {
        const text = (heading.innerText || '').trim();
        if (text && text.length >= 3) return text;
      }
      node = node.parentElement;
    }
    return null;
  };

  for (const selector of selectors) {
    let nodes = [];
    try { nodes = document.querySelectorAll(selector); } catch (e) { continue; }
    for (const el of nodes) {
      const href = el.getAttribute('href');
      if (!href) continue;
      let title = (el.innerText || el.textContent || '').trim();
      if (genericCtaRe.test(title)) {
        title = nearbyHeading(el) || title;
      }
      if (!title || title.length < 3) continue;
      const key = href + '|' + title;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ title, href, location: nearbyLocation(el), date: nearbyDate(el) });
    }
  }
  return out;
}
"""


@dataclass
class PlaywrightResult:
    """Outcome of rendering one company's career page."""

    jobs: list[dict[str, Any]] = field(default_factory=list)
    discovered_ats_url: str | None = None
    discovered_provider: str | None = None
    #: Search terms actually submitted, in order. Empty when no search ran.
    queries_run: list[str] = field(default_factory=list)
    #: True when the page answered with a bot challenge, login wall or explicit
    #: denial. Such a company is recorded and left alone rather than retried
    #: with a different fingerprint - see :func:`_looks_blocked`.
    blocked: bool = False


def _start_playwright(use_stealth: bool):
    """Start Playwright, optionally with stealth hooks installed.

    ``playwright_stealth.Stealth.use_sync`` must wrap the Playwright factory
    itself: it hooks context creation so every new context gets consistent
    UA/`sec-ch-ua`/header patching, not just page-level JS shims. Patching
    only the page (``apply_stealth_sync``) is measurably weaker - against
    GameStop's Cloudflare challenge, page-only patching still returned the
    challenge page, while the hooked form returned the real careers page.

    Returns ``(playwright, context_manager_or_None)``; the context manager
    must be exited during teardown when present.
    """
    from playwright.sync_api import sync_playwright

    if use_stealth:
        try:
            from playwright_stealth import Stealth
            manager = Stealth().use_sync(sync_playwright())
            return manager.__enter__(), manager
        except Exception as exc:  # pragma: no cover - optional dependency
            log.debug("playwright-stealth unavailable (%s); continuing unpatched", exc)

    return sync_playwright().start(), None


def _get_browser():
    """Return this thread's Chromium instance, launching it on first use."""
    browser = getattr(_thread_local, "browser", None)
    if browser is not None and browser.is_connected():
        return browser

    cfg = load_settings()
    headless = bool(cfg.get("playwright.headless", True))
    use_stealth = bool(cfg.get("playwright.stealth", True))

    playwright, manager = _start_playwright(use_stealth)
    try:
        browser = playwright.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
    except Exception:
        # The driver (and its event loop) is already running by the time launch
        # is attempted. If launch fails we must tear it down here - leaving it
        # started but unreferenced poisons the next _start_playwright on this
        # thread with "Sync API inside the asyncio loop". Mirror the teardown
        # order used in shutdown_thread_browser.
        try:
            if manager is not None:
                manager.__exit__(None, None, None)
            else:
                playwright.stop()
        except Exception as exc:  # pragma: no cover - teardown best effort
            log.debug("Playwright cleanup after failed launch failed: %s", exc)
        raise

    _thread_local.playwright = playwright
    _thread_local.browser = browser
    _thread_local.manager = manager
    with _instances_lock:
        _all_instances.append((threading.get_ident(), playwright, browser, manager))

    log.debug("Launched Chromium for thread %s (stealth=%s)",
              threading.current_thread().name, manager is not None)
    return browser


def shutdown_thread_browser() -> None:
    """Close the Playwright instance owned by the *calling* thread.

    Playwright's sync API is thread-affine: its objects belong to the thread
    (and greenlet) that created them, so tearing one down from another thread
    can deadlock rather than error. This is not theoretical - closing worker
    browsers from the main thread wedged a full run indefinitely after
    scraping had finished, leaving ~100 orphaned Chromium processes. Each
    worker must therefore close its own instance.
    """
    browser = getattr(_thread_local, "browser", None)
    playwright = getattr(_thread_local, "playwright", None)
    manager = getattr(_thread_local, "manager", None)

    if browser is None and playwright is None:
        return

    ident = threading.get_ident()
    with _instances_lock:
        _all_instances[:] = [row for row in _all_instances if row[0] != ident]

    try:
        if browser is not None and browser.is_connected():
            browser.close()
    except Exception as exc:  # pragma: no cover - teardown best effort
        log.debug("Browser close failed: %s", exc)

    try:
        # A stealth-hooked Playwright is owned by its context manager and
        # must be exited rather than stopped directly.
        if manager is not None:
            manager.__exit__(None, None, None)
        elif playwright is not None:
            playwright.stop()
    except Exception as exc:  # pragma: no cover
        log.debug("Playwright stop failed: %s", exc)

    _thread_local.browser = None
    _thread_local.playwright = None
    _thread_local.manager = None


def shutdown_browsers() -> None:
    """Close this thread's browser, and report any left owned by other threads.

    Prefer :func:`shutdown_thread_browser` from inside each worker (see
    ``pipeline.execute_plans``). Cross-thread teardown is deliberately *not*
    attempted here - it deadlocks - so leftovers are only logged; they are
    reaped when the process exits.
    """
    shutdown_thread_browser()

    with _instances_lock:
        leftover = len(_all_instances)
    if leftover:
        log.debug("%s browser instance(s) still owned by other threads; "
                  "they will be released when those threads finish", leftover)


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    collapsed = " ".join(str(text).split())
    return collapsed or None


def _clean_location(text: str | None) -> str | None:
    """Strip the visible field label many career sites render before the value."""
    cleaned = _clean(text)
    if not cleaned:
        return None
    cleaned = re.sub(r"^(job\s+)?locations?\s*[:\-]?\s*", "", cleaned, flags=re.I)
    return cleaned or None


def _is_job_row(title: str | None, href: str | None) -> bool:
    """Filter navigation chrome out of the extracted anchor list."""
    if not title or not href:
        return False
    lowered = title.strip().lower()
    if len(lowered) < 6 or len(lowered) > 250:
        return False
    if _is_nav_text(title):
        return False
    if href.startswith(("javascript:", "mailto:", "tel:")):
        return False
    # A bare "#" or "#section-name" is an in-page anchor, not a job link -
    # but a hash-routed SPA path is a real (if client-side) route and must
    # survive. Confirmed live on two different frameworks: Kforce uses
    # "#/detail/{id}/" (leading slash), Mphasis's RippleHire widget uses
    # "#detail/job/{id}" (no leading slash) - both have real path segments
    # after the "#", which a plain same-page anchor ("#top", "#about-us")
    # never does, so "any slash after the #" is the general signal.
    if href.startswith("#") and "/" not in href[1:]:
        return False
    return True


def _paginate_and_extract(
    page, initial_rows: list[dict[str, Any]], max_clicks: int, timeout_ms: int
) -> tuple[list[dict[str, Any]], bool]:
    """Click through "Load more"/next controls, accumulating every page's
    rows rather than keeping only whatever is on screen after the last click.

    Some sites append new rows to the same page ("Load more" - the visible
    list only grows). Others replace the page's content entirely with each
    click (a genuine "next page" link, matched here by the same selector
    list) - extracting only once, after all clicking is done, would then
    silently keep just the final page and discard everything before it.
    Confirmed live on Goldman Sachs' career site: its "next" control replaces
    the list outright (1 of 20 jobs still present after a single click), so
    the old extract-once-at-the-end approach returned only the last of 6
    pages actually visited, not the ~120 jobs across all of them.

    Re-extracting and merging by job_url after every click/scroll handles
    both patterns without needing to know which one a given site uses.
    Takes the caller's own initial extraction rather than re-running it, so
    the "don't click at all when the page starts out empty" guard at each
    call site stays intact (blind clicking on a jobless page can navigate
    away and destroy UI the caller needs next - observed on Goldman Sachs).

    Returns:
        ``(rows, exhausted)``. ``exhausted`` is False when the click budget
        ran out with pages still to go - the browser equivalent of a truncated
        API walk, and previously indistinguishable from a site that simply had
        no more results.
    """
    seen: dict[str, dict[str, Any]] = {}

    def _merge(rows: list[dict[str, Any]]) -> int:
        added = 0
        for row in rows:
            key = row.get("job_url")
            if key and key not in seen:
                seen[key] = row
                added += 1
        return added

    _merge(initial_rows)

    # Two consecutive clicks that add nothing mean the control is inert - a
    # button that stays visible but no longer loads anything. One barren click
    # is tolerated because some sites render asynchronously.
    barren = 0

    for _ in range(max_clicks):
        clicked = False
        for selector in LOAD_MORE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=1500):
                    continue
                locator.scroll_into_view_if_needed(timeout=3000)
                locator.click(timeout=5000)
                page.wait_for_timeout(min(timeout_ms // 10, 2500))
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            # No pagination control: try lazy-load via scrolling instead.
            try:
                before = page.evaluate("document.body.scrollHeight")
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(1200)
                after = page.evaluate("document.body.scrollHeight")
                if after <= before:
                    return list(seen.values()), True
                clicked = True
            except Exception:
                return list(seen.values()), True

        if _merge(_extract_job_rows(page)):
            barren = 0
        else:
            barren += 1
            if barren >= 2:
                return list(seen.values()), True

    # The budget ran out while the control was still producing rows, so there
    # is very likely more to collect.
    return list(seen.values()), False


def _extract_jsonld_rows(page) -> list[dict[str, Any]]:
    """Read schema.org JobPosting data embedded in the rendered page.

    Many career sites emit JSON-LD for SEO even when their visible job list is
    client-rendered behind a search box - and unlike scraped anchors, JSON-LD
    carries a real ``datePosted``, which the freshness filter needs.
    """
    try:
        html_text = page.content()
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for node in iter_jsonld_jobs(html_text):
        title = _clean(node.get("title"))
        url = node.get("url") or node.get("@id")
        if not title or not url:
            continue
        absolute = urljoin(page.url, str(url))
        if absolute in seen:
            continue
        seen.add(absolute)
        rows.append({
            "title": title,
            "location": jsonld_location(node),
            "job_url": absolute,
            "date_posted": node.get("datePosted"),
        })

    if rows:
        log.debug("Extracted %s job(s) from embedded JSON-LD", len(rows))
    return rows


def _extract_job_rows(page) -> list[dict[str, Any]]:
    """Run the DOM extraction script and turn it into raw job dicts."""
    try:
        raw_rows = page.evaluate(_EXTRACT_JS, list(JOB_LINK_SELECTORS))
    except Exception as exc:
        log.warning("DOM extraction failed (%s)", exc)
        raw_rows = []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in raw_rows or []:
        title = _clean(row.get("title"))
        href = row.get("href")
        if not _is_job_row(title, href):
            continue
        absolute = urljoin(page.url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        results.append({
            "title": title,
            "location": _clean_location(row.get("location")),
            "job_url": absolute,
            "date_posted": row.get("date"),
        })
    return results


_JOBS_LINK_JS = """
(args) => {
  const [texts, hrefHints] = args;
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (!href || href.startsWith('#') || href.startsWith('javascript:')) continue;
    const text = ((a.innerText || a.textContent || '') + ' ' +
                  (a.getAttribute('aria-label') || '')).trim().toLowerCase();
    const lowerHref = href.toLowerCase();

    let score = 0;
    for (const t of texts) {
      if (text === t) { score = Math.max(score, 100); }
      else if (text.includes(t)) { score = Math.max(score, 70); }
    }
    for (const h of hrefHints) {
      if (lowerHref.includes(h)) { score = Math.max(score, score + 25); }
    }
    // Prefer links that look like a listing rather than a single posting.
    if (/\\/job\\/|\\/jobs\\/\\d|jobid=/i.test(lowerHref)) score -= 40;
    if (score > 0) out.push({ href, text: text.slice(0, 60), score });
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, 5);
}
"""


def _hop_key(url: str) -> str:
    """Normalized identity for visit-deduplication during traversal.

    Case and a trailing slash must not make the same page look new, or the
    traversal can loop between "/jobs" and "/jobs/" until the budget expires.
    """
    return url.split("#")[0].rstrip("/").lower()


def _find_jobs_page_links(page) -> list[dict[str, Any]]:
    """Rank links on a careers landing page that lead to the actual job list."""
    try:
        return page.evaluate(
            _JOBS_LINK_JS, [list(JOBS_PAGE_LINK_TEXT), list(JOBS_PAGE_HREF_HINTS)]
        ) or []
    except Exception as exc:
        log.debug("Jobs-page link scan failed (%s)", exc)
        return []


def _navigate_to_job_list(
    company: str, page, timeout_ms: int, max_hops: int | None = None
) -> PlaywrightResult:
    """Traverse a careers site until a page yields jobs or reveals an ATS.

    Many workbook URLs point at a marketing careers page whose openings live
    one or more links away (IBM -> /careers/search, Centene -> /us/en/jobs ->
    the list) or on a different ATS host entirely (GameStop ->
    gamestop.rec.pro.ukg.net). Because the ATS case is so valuable, every
    candidate link is checked with :func:`detect_ats` *before* navigating:
    recognising the ATS is strictly better than scraping its HTML, so the URL
    is handed straight back as a discovery for the router to collect properly.

    Traversal is best-first on the link scores from
    :func:`_find_jobs_page_links` and bounded by depth, total visits and a
    wall-clock budget, so a sprawling site cannot consume the per-company
    timeout.
    """
    cfg = load_settings()
    if max_hops is None:
        max_hops = int(cfg.get("playwright.max_hops", 5))
    max_visits = int(cfg.get("playwright.max_hop_visits", 12))
    budget_s = float(cfg.get("playwright.hop_budget_seconds", 100))
    settle_ms = int(cfg.get("playwright.wait_after_load_ms", 2500))
    max_pages = int(cfg.get("playwright.max_pages", 5))
    search_each = bool(cfg.get("playwright.search_at_each_hop", True))
    good_enough = int(cfg.get("playwright.hop_good_enough_rows", 10))

    deadline = time.monotonic() + budget_s
    visited: set[str] = {_hop_key(page.url)}
    visits = 0
    # Best partial result seen so far. A page showing three featured roles
    # is worth keeping, but not worth stopping the search for.
    best = PlaywrightResult()

    # Frontier entries are (depth, url, score); higher score explored first.
    frontier: list[tuple[int, str, int]] = []

    def _enqueue(depth: int) -> None:
        for candidate in _find_jobs_page_links(page):
            target = urljoin(page.url, candidate["href"])
            if _hop_key(target) in visited:
                continue
            frontier.append((depth, target, int(candidate.get("score", 0))))

    _enqueue(1)

    while frontier and visits < max_visits and time.monotonic() < deadline:
        # An ATS link anywhere in the frontier beats scraping any HTML.
        for _depth, target, _score in frontier:
            detection = detect_ats(target)
            if detection["provider"] != UNKNOWN:
                log.info("%s: careers page links to %s -> %s",
                         company, detection["provider"], target[:90])
                return PlaywrightResult(
                    discovered_ats_url=target,
                    discovered_provider=detection["provider"],
                )

        frontier.sort(key=lambda entry: (-entry[2], entry[0]))
        depth, target, _score = frontier.pop(0)

        key = _hop_key(target)
        if key in visited:
            continue
        visited.add(key)

        try:
            page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            log.debug("%s: hop to %s failed (%s)", company, target[:80], exc)
            continue
        visits += 1

        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
        except Exception:
            pass
        page.wait_for_timeout(settle_ms)
        _dismiss_cookie_banner(page)

        # Same ordering rule as the landing page: paginate only once a list
        # is actually present.
        rows = _extract_job_rows(page)
        if rows:
            rows, _exhausted = _paginate_and_extract(page, rows, max_pages, timeout_ms)
        else:
            rows = _extract_jsonld_rows(page)
        if len(rows) >= good_enough:
            log.info("%s: found %s jobs at depth %s -> %s",
                     company, len(rows), depth, target[:90])
            return PlaywrightResult(jobs=rows)
        if len(rows) > len(best.jobs):
            best = PlaywrightResult(jobs=rows)

        # This page may be search-driven: the list renders only after a
        # keyword is submitted. Cheap relative to another navigation.
        if search_each and time.monotonic() < deadline:
            searched = _search_fallback(company, page, timeout_ms)
            if searched.discovered_provider:
                return searched
            if len(searched.jobs) >= good_enough:
                log.info("%s: found %s jobs via search at depth %s -> %s",
                         company, len(searched.jobs), depth, target[:90])
                return searched
            if len(searched.jobs) > len(best.jobs):
                best = searched

        if depth < max_hops:
            _enqueue(depth + 1)

    if best.jobs:
        log.info("%s: traversal settled on %s job(s)", company, len(best.jobs))
    return best


# Markers of a page that is refusing automated access rather than failing.
# Kept deliberately specific: matching loosely would misread an ordinary
# careers page that happens to mention "security" or "verification".
_BLOCK_MARKERS = (
    "verify you are human",
    "are you a human",
    "i am not a robot",
    "checking your browser",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "access denied",
    "request unsuccessful",
    "unusual traffic",
    "captcha",
    "px-captcha",
    "distil_r_blocked",
)


def _looks_blocked(page) -> bool:
    """True when the page is a bot challenge, denial or CAPTCHA.

    This exists so a blocked site is **recorded and left alone** rather than
    retried with a rotated fingerprint. A challenge page renders cleanly with
    zero jobs, so without this it took the "clean render, no jobs" retry path -
    three full traversals, up to eight minutes, before being written down as a
    generic zero-jobs failure that told nobody what actually happened.

    Detection only. Nothing here attempts to solve or bypass a challenge: when
    a site says it does not want automated access, the honest outcomes are to
    record it and move on.
    """
    try:
        title = (page.title() or "").lower()
        # Only the visible text - matching raw HTML would hit analytics and
        # consent scripts that mention "captcha" on perfectly normal pages.
        body = page.inner_text("body")[:4000].lower()
    except Exception:
        return False

    haystack = f"{title} {body}"
    return any(marker in haystack for marker in _BLOCK_MARKERS)


def _dismiss_cookie_banner(page) -> None:
    """Best-effort click on a consent banner across every frame on the page."""
    for frame in page.frames:
        for selector in COOKIE_CONSENT_SELECTORS:
            try:
                locator = frame.locator(selector).first
                if locator.count() == 0 or not locator.is_visible(timeout=800):
                    continue
                locator.click(timeout=2000)
                page.wait_for_timeout(400)
                return
            except Exception:
                continue


def _find_search_input(page):
    """Find a job-keyword search input, scanning every frame.

    Returns ``(frame, locator)`` or ``None``. Explicitly rejects inputs that
    look like a location field even if they also loosely match a search hint.
    """
    for frame in page.frames:
        try:
            candidates = frame.locator(
                'input[type="text"], input[type="search"], input:not([type])'
            )
            count = min(candidates.count(), 20)
        except Exception as e:
            continue

        for index in range(count):
            locator = candidates.nth(index)
            try:
                if not locator.is_visible(timeout=500):
                    continue
                placeholder = locator.get_attribute("placeholder") or ""
                aria_label = locator.get_attribute("aria-label") or ""
                name = locator.get_attribute("name") or ""
                input_id = locator.get_attribute("id") or ""
                haystack = f"{placeholder} {aria_label} {name} {input_id}"

                search_hint = _SEARCH_INPUT_HINT_RE.search(haystack)
                # A field that hints at both ("Search by city, zip, or
                # role") is a single combined location+keyword box, not a
                # location-only field - confirmed live: Pyramid Consulting's
                # only input has exactly this placeholder, and rejecting it
                # outright left the site with no usable search input at all.
                if _LOCATION_INPUT_HINT_RE.search(haystack) and not search_hint:
                    continue
                if search_hint:
                    return frame, locator
            except Exception as e:
                continue
    return None


def _submit_search(page, frame, locator, term: str) -> bool:
    """Type ``term`` into ``locator`` and submit. Returns True if it tried."""
    try:
        locator.click(timeout=3000)
        locator.fill(term, timeout=3000)
    except Exception as exc:
        log.debug("Could not type into search input (%s)", exc)
        return False

    submitted = False
    try:
        locator.press("Enter", timeout=3000)
        submitted = True
    except Exception:
        pass

    if not submitted:
        for selector in ('button:has-text("Search")', 'button[type="submit"]', 'button[aria-label*="search" i]'):
            try:
                button = frame.locator(selector).first
                if button.count() and button.is_visible(timeout=500):
                    button.click(timeout=3000)
                    submitted = True
                    break
            except Exception:
                continue

    return submitted


def _sniff_ats_from_urls(urls: list[str]) -> tuple[str | None, str | None]:
    """Check captured network URLs for a recognizable ATS endpoint.

    Only accepts the discovery when every recognized URL agrees on the same
    tenant+site - a page that calls two regional tenants of the same ATS
    (observed on FedEx: separate XHRs to a US and a MEISA Workday tenant) must
    not have "whichever request happened to fire first" picked as the answer.
    """
    resolved = [detect_ats(u) for u in urls]
    resolved = [r for r in resolved if r["provider"] != UNKNOWN]
    if not resolved:
        return None, None

    # Judge only the first-seen provider, matching the previous first-match
    # behaviour when there is nothing ambiguous to resolve.
    provider = resolved[0]["provider"]
    same_provider = [r for r in resolved if r["provider"] == provider]
    distinct = {(r["tenant"], r["site"]) for r in same_provider}

    if len(distinct) == 1:
        return resolved[0]["url"], provider

    log.debug(
        "Network sniffing found %s requests pointing at %s different %s "
        "tenants; discarding (ambiguous)",
        len(same_provider), len(distinct), provider,
    )
    return None, None


def _discover_host_based_ats(page) -> PlaywrightResult | None:
    """Recognise an ATS that runs on the company's own domain, by fingerprint.

    Some platforms (Radancy TalentBrew) render on ``careers.{company}.com``
    with no vendor host in the URL and load their job list over XHR the DOM
    scraper cannot see. When the rendered HTML fingerprints as one of these,
    hand the page URL back as a discovery so the router's self-healing path
    drives the real collector - strictly better than scraping the DOM.
    """
    try:
        provider = detect_from_html(page.content(), final_url=page.url)
    except Exception:
        return None
    if provider != RADANCY:
        return None
    return PlaywrightResult(discovered_ats_url=page.url, discovered_provider=provider)


def _configured_search_terms() -> list[str]:
    """The query list, tolerating the older single-term config key.

    One term was never enough: a role whose title contains no "data" - Snowflake
    Engineer, Databricks Engineer, ETL Developer, Analytics Engineer - is
    invisible to a "Data" search, and on a search-driven site the site's own
    search is the only view of its jobs we ever get.
    """
    cfg = load_settings()
    terms = cfg.get("playwright.search_fallback.search_terms")
    if isinstance(terms, list) and terms:
        return [str(t) for t in terms if str(t).strip()]
    single = cfg.get("playwright.search_fallback.search_term", "Data")
    return [str(single)]


def _search_fallback(
    company: str,
    page,
    timeout_ms: int,
    *,
    search_terms: list[str] | None = None,
    max_queries: int | None = None,
) -> PlaywrightResult:
    """Submit each configured search term, merging what every query returns.

    Called when a page has not already yielded a real job list. Each query is
    typed into the detected keyword input (``fill`` replaces the previous
    value, so the box is reused rather than re-found), submitted, and the
    results re-extracted. Rows merge by ``job_url`` across queries and carry
    ``source_query`` so the output records which term found each job.

    While this runs it also watches network traffic for a real ATS endpoint the
    static-HTML resolver never saw - many custom career pages only call their
    ATS once you interact with them.
    """
    cfg = load_settings()
    terms = search_terms if search_terms is not None else _configured_search_terms()
    limit = max_queries if max_queries is not None else int(
        cfg.get("playwright.search_fallback.max_queries", 4)
    )
    terms = terms[:max(1, limit)]
    max_wait_ms = int(cfg.get("playwright.search_fallback.max_wait_ms", 6000))
    good_enough = int(cfg.get("playwright.hop_good_enough_rows", 10))

    found = _find_search_input(page)
    if found is None:
        log.debug("%s: no search input found for fallback", company)
        return PlaywrightResult()

    frame, locator = found

    seen_urls: list[str] = []

    def _record_response(response) -> None:
        # Bounded: a busy page can emit thousands of responses, and only the
        # ATS-shaped ones matter.
        if len(seen_urls) < 400:
            seen_urls.append(response.url)

    merged: dict[str, dict[str, Any]] = {}
    queries_run: list[str] = []

    page.on("response", _record_response)
    try:
        _dismiss_cookie_banner(page)

        for term in terms:
            if not _submit_search(page, frame, locator, term):
                # The input went away (a search can navigate elsewhere); try to
                # find it again on the new page before giving up.
                found = _find_search_input(page)
                if found is None:
                    break
                frame, locator = found
                if not _submit_search(page, frame, locator, term):
                    break

            queries_run.append(term)

            try:
                page.wait_for_load_state(
                    "networkidle", timeout=min(max_wait_ms, timeout_ms)
                )
            except Exception:
                pass
            page.wait_for_timeout(max_wait_ms)

            if _looks_blocked(page):
                log.warning("%s: search blocked by the site; stopping", company)
                page.remove_listener("response", _record_response)
                return PlaywrightResult(
                    jobs=list(merged.values()), queries_run=queries_run, blocked=True,
                )

            rows, _exhausted = _paginate_and_extract(
                page, _extract_job_rows(page),
                int(cfg.get("playwright.max_pages", 10)), timeout_ms,
            )
            for row in rows:
                url = row.get("job_url")
                if url and url not in merged:
                    row["source_query"] = term
                    merged[url] = row

            # Enough is enough: a query list is insurance against a narrow
            # first term, not a reason to keep searching a site that answered.
            if len(merged) >= good_enough and len(queries_run) >= 1 and term != terms[-1]:
                if len(merged) >= good_enough * 3:
                    break
    finally:
        try:
            page.remove_listener("response", _record_response)
        except Exception:
            pass

    discovered_url, discovered_provider = _sniff_ats_from_urls(seen_urls)

    if not discovered_provider:
        try:
            html_text = page.content()
        except Exception:
            html_text = ""

        # A bare fingerprint match (the word "workday" appearing somewhere in
        # the page - a widget, an ad script, unrelated text) is not
        # actionable: it gives a provider name but no concrete URL a
        # collector could actually use. Reporting page.url itself here
        # previously produced a false positive (FedEx: matched "workday" in
        # page HTML, reported the FedEx search page itself as a "Workday
        # URL", which no collector could parse).
        provider = detect_from_html(html_text, final_url=page.url)
        if provider != UNKNOWN:
            candidates = extract_all_embedded_ats_urls(html_text, provider)
            # Large companies often run several regional tenants of the same
            # ATS (confirmed on FedEx: a single search-results page embedded
            # both a US-tenant link and an India/MEISA-tenant link). Picking
            # "the first match" silently locks future runs onto whichever
            # region happened to render first. Only accept the discovery when
            # every embedded candidate resolves to the same tenant+site.
            resolved = [detect_ats(c) for c in candidates]
            resolved = [r for r in resolved if r["provider"] == provider]
            distinct = {(r["tenant"], r["site"]) for r in resolved}

            if len(distinct) == 1 and resolved:
                discovered_provider = provider
                discovered_url = resolved[0]["url"]
            elif len(distinct) > 1:
                log.debug(
                    "%s: HTML fingerprint suggested %s but embedded URLs point "
                    "at %s different tenants; discarding (ambiguous)",
                    company, provider, len(distinct),
                )
            else:
                log.debug(
                    "%s: HTML fingerprint suggested %s but no concrete URL "
                    "could be extracted; discarding (not actionable)",
                    company, provider,
                )

    jobs = list(merged.values())

    if jobs or discovered_provider:
        log.info(
            "%s: search fallback (%s) found %s jobs%s",
            company, ", ".join(repr(q) for q in queries_run), len(jobs),
            f", discovered ATS={discovered_provider}" if discovered_provider else "",
        )

    return PlaywrightResult(
        jobs=jobs, discovered_ats_url=discovered_url,
        discovered_provider=discovered_provider, queries_run=queries_run,
    )


def _scrape_once(company: str, url: str, attempt: int) -> PlaywrightResult:
    """One render+extract attempt. Raises RuntimeError if navigation fails."""
    cfg = load_settings()
    timeout_ms = int(cfg.get("playwright.timeout_ms", 30000))
    max_pages = int(cfg.get("playwright.max_pages", 5))
    settle_ms = int(cfg.get("playwright.wait_after_load_ms", 2500))
    search_enabled = bool(cfg.get("playwright.search_fallback.enabled", True))

    browser = _get_browser()
    context = None
    page = None

    # Rotate the fingerprint on retries: several sites reset the connection
    # for a repeat visitor with an identical UA/viewport.
    user_agent = cfg.get("playwright.user_agent") or cfg.get("requests.user_agent")
    viewport = {"width": 1440, "height": 900}
    if attempt > 0:
        user_agent = random.choice(RETRY_USER_AGENTS)
        viewport = random.choice(RETRY_VIEWPORTS)

    try:
        context = browser.new_context(
            viewport=viewport,
            user_agent=user_agent,
            ignore_https_errors=True,
            java_script_enabled=True,
            locale="en-US",
        )
        context.set_default_timeout(timeout_ms)
        page = context.new_page()

        # Block heavy assets: 2-4x faster, and we only need the DOM.
        def _block(route):
            if route.request.resource_type in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", _block)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            raise RuntimeError(f"Navigation failed for {url}: {exc}") from exc

        # Let client-side rendering settle; networkidle often never fires on
        # analytics-heavy career sites, so a timeout here is not fatal.
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
        except Exception:
            pass
        page.wait_for_timeout(settle_ms)

        _dismiss_cookie_banner(page)

        # Stop immediately on a challenge or denial: nothing below can succeed
        # against one, and the retry loop must not treat it as a flaky render.
        if _looks_blocked(page):
            log.warning("%s: %s answered with a challenge or denial",
                        company, urlsplit(page.url).netloc)
            return PlaywrightResult(blocked=True)

        # A branded-domain ATS (Radancy TalentBrew) is recognised only from the
        # rendered HTML. Detecting it here lets the router self-heal to the real
        # collector instead of scraping an XHR-driven list the DOM cannot show.
        host_based = _discover_host_based_ats(page)
        if host_based is not None:
            return host_based

        good_enough = int(cfg.get("playwright.hop_good_enough_rows", 10))
        # Landing pages routinely show a handful of "featured" roles. Taking
        # those and stopping would report 3 jobs for a company with
        # thousands, so a small result is kept only as a fallback while the
        # search and hop paths look for the real list.
        best = PlaywrightResult()

        # Extract before paginating. "Load more"/"next" only mean something
        # once a list exists; clicking them on a page with no jobs is blind
        # clicking that can navigate away and destroy the very search box the
        # fallback needs next - observed on Goldman Sachs, where four such
        # clicks left the page with no search input at all.
        jobs = _extract_job_rows(page)
        if jobs:
            initial_count = len(jobs)
            jobs, exhausted = _paginate_and_extract(page, jobs, max_pages, timeout_ms)
            if not exhausted:
                log.warning("%s: pagination stopped at the %s-page cap with more "
                            "results still available", company, max_pages)
            if len(jobs) != initial_count:
                log.debug("%s: pagination changed the result count %s -> %s",
                          company, initial_count, len(jobs))
        else:
            # JSON-LD is often present even when the visible list is
            # client-rendered, and it carries real posting dates.
            jobs = _extract_jsonld_rows(page)

        log.debug("%s: Playwright extracted %s job rows", company, len(jobs))

        if len(jobs) >= good_enough:
            return PlaywrightResult(jobs=jobs)
        if len(jobs) > len(best.jobs):
            best = PlaywrightResult(jobs=jobs)

        # Try the search box here first (cheap, no navigation), then hop to a
        # dedicated job-list page.
        landing_url = page.url
        if search_enabled:
            result = _search_fallback(company, page, timeout_ms)
            if result.discovered_provider:
                return result
            if len(result.jobs) >= good_enough:
                return result
            if len(result.jobs) > len(best.jobs):
                best = result

        # A failed search leaves the page filtered or navigated elsewhere, so
        # its links no longer describe the careers site. Traversal must start
        # from the landing page it was meant to explore.
        if page.url != landing_url:
            try:
                page.goto(landing_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(settle_ms)
                _dismiss_cookie_banner(page)
            except Exception as exc:
                log.debug("%s: could not return to %s (%s)", company, landing_url[:80], exc)

        hopped = _navigate_to_job_list(company, page, timeout_ms)
        if hopped.discovered_provider:
            return hopped
        if len(hopped.jobs) >= good_enough:
            return hopped
        if len(hopped.jobs) > len(best.jobs):
            best = hopped

        return best

    finally:
        for closeable in (page, context):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception as exc:  # pragma: no cover - teardown best effort
                log.debug("%s: cleanup failed (%s)", company, exc)


def scrape_with_playwright(company: str, url: str) -> PlaywrightResult:
    """Render ``url`` and extract job rows, retrying transient failures.

    Returns:
        A :class:`PlaywrightResult`. ``jobs`` entries are deliberately raw
        ``{"title", "location", "job_url", "date_posted"}`` dicts - the router
        normalizes them. ``date_posted`` is not extracted because rendered
        career pages rarely expose a trustworthy date, and inventing one would
        corrupt the freshness filter.

        When the initial load finds nothing, a single search-fallback attempt
        runs (see :func:`_search_fallback`): it also watches network traffic
        for a real ATS endpoint the static-HTML resolver could not see, and
        reports it via ``discovered_ats_url``/``discovered_provider`` so the
        pipeline can write it back to the input workbook for future runs.

    Navigation errors are retried with a fresh context and rotated
    fingerprint. Most are transient rather than real: an earlier full run
    recorded 11 companies as ERR_NAME_NOT_RESOLVED whose domains all resolved
    fine on retry - Chromium's resolver simply buckled under concurrent
    browser instances.

    A clean render that comes back with zero jobs (no exception, no
    discovery) gets the same retry treatment, up to the same attempt budget.
    Confirmed against a same-day pair of full runs: Nokia, Ericsson and CBRE
    each rendered fine and found real jobs in one run, then came back empty
    in the other under the same 3-worker concurrency - re-verified
    individually afterward, all three worked every time in isolation. A
    fresh context and rotated fingerprint costs nothing when the page
    already has jobs (this path never runs then) and gives the flaky case a
    second chance instead of recording a company as failed on one bad draw.

    Raises:
        RuntimeError: navigation failed on every attempt.
    """
    cfg = load_settings()
    attempts = max(1, int(cfg.get("playwright.nav_retries", 3)))
    backoff_ms = int(cfg.get("playwright.nav_retry_backoff_ms", 2000))

    last_error: Exception | None = None
    last_empty: PlaywrightResult | None = None
    for attempt in range(attempts):
        try:
            result = _scrape_once(company, url, attempt)
        except RuntimeError as exc:
            last_error = exc
            if attempt < attempts - 1:
                delay = (backoff_ms * (attempt + 1) + random.randint(0, 750)) / 1000
                log.debug("%s: navigation attempt %s failed (%s); retrying in %.1fs",
                          company, attempt + 1, str(exc)[:90], delay)
                time.sleep(delay)
            continue

        if result.jobs or result.discovered_provider:
            return result

        # An explicit refusal is a final answer, not a bad draw. Retrying it
        # with a rotated user-agent and viewport would be varying our identity
        # to get past a control the site stated plainly - and it does not work
        # anyway, so it only costs the per-company budget.
        if result.blocked:
            log.warning("%s: site refused automated access; recording and moving on",
                        company)
            return result

        last_empty = result
        if attempt < attempts - 1:
            delay = (backoff_ms * (attempt + 1) + random.randint(0, 750)) / 1000
            log.debug("%s: attempt %s rendered cleanly but found no jobs; retrying in %.1fs",
                      company, attempt + 1, delay)
            time.sleep(delay)

    if last_empty is not None:
        return last_empty
    raise last_error if last_error else RuntimeError(f"Navigation failed for {url}")
