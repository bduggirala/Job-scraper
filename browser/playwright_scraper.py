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
    'a[data-ph-at-id="job-link"]',
    'a[data-automation-id*="jobTitle"]',
    "a.job-title-link",
    "a.jobTitle",
    "a.job-title",
    "a.jobs-list-item__link",
    '[class*="job-card"] a',
    '[class*="jobCard"] a',
    '[class*="job-result"] a',
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
_SEARCH_INPUT_HINT_RE = re.compile(r"search|keyword|job.?title|role|position|what", re.I)
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

  for (const selector of selectors) {
    let nodes = [];
    try { nodes = document.querySelectorAll(selector); } catch (e) { continue; }
    for (const el of nodes) {
      const href = el.getAttribute('href');
      if (!href) continue;
      const title = (el.innerText || el.textContent || '').trim();
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
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )

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
    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    return True


def _click_load_more(page, max_clicks: int, timeout_ms: int) -> int:
    """Click through "Load more"/next controls. Returns the number of clicks."""
    clicks = 0
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
                clicks += 1
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
                    break
                clicks += 1
            except Exception:
                break
    return clicks


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


def _find_jobs_page_links(page) -> list[dict[str, Any]]:
    """Rank links on a careers landing page that lead to the actual job list."""
    try:
        return page.evaluate(
            _JOBS_LINK_JS, [list(JOBS_PAGE_LINK_TEXT), list(JOBS_PAGE_HREF_HINTS)]
        ) or []
    except Exception as exc:
        log.debug("Jobs-page link scan failed (%s)", exc)
        return []


def _navigate_to_job_list(company: str, page, timeout_ms: int, max_hops: int = 2) -> PlaywrightResult:
    """Follow "Search jobs"-style links until a page yields jobs or an ATS.

    Many workbook URLs point at a marketing careers page whose openings live
    one hop away (IBM -> /careers/search) or on a different ATS host entirely
    (GameStop -> gamestop.rec.pro.ukg.net). Because the second case is so
    common, each candidate link is checked against :func:`detect_ats` *before*
    navigating: recognising the ATS is strictly better than scraping its HTML,
    so the URL is handed straight back as a discovery for the router to
    collect properly.
    """
    settle_ms = int(load_settings().get("playwright.wait_after_load_ms", 2500))
    visited: set[str] = {page.url}

    for hop in range(max_hops):
        candidates = _find_jobs_page_links(page)
        if not candidates:
            return PlaywrightResult()

        # An ATS link anywhere in the candidate set beats scraping HTML.
        for candidate in candidates:
            target = urljoin(page.url, candidate["href"])
            detection = detect_ats(target)
            if detection["provider"] != UNKNOWN:
                log.info("%s: careers page links to %s -> %s",
                         company, detection["provider"], target[:90])
                return PlaywrightResult(
                    discovered_ats_url=target,
                    discovered_provider=detection["provider"],
                )

        for candidate in candidates:
            target = urljoin(page.url, candidate["href"])
            if target in visited:
                continue
            visited.add(target)

            try:
                page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                log.debug("%s: hop to %s failed (%s)", company, target[:80], exc)
                continue

            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
            except Exception:
                pass
            page.wait_for_timeout(settle_ms)
            _dismiss_cookie_banner(page)

            _click_load_more(page, int(load_settings().get("playwright.max_pages", 5)), timeout_ms)
            rows = _extract_job_rows(page)
            if rows:
                log.info("%s: found %s jobs after hop %s -> %s",
                         company, len(rows), hop + 1, target[:90])
                return PlaywrightResult(jobs=rows)
            # No rows here, but this page may itself link deeper; loop again.
            break

    return PlaywrightResult()


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
        except Exception:
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

                if _LOCATION_INPUT_HINT_RE.search(haystack):
                    continue
                if _SEARCH_INPUT_HINT_RE.search(haystack):
                    return frame, locator
            except Exception:
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


def _search_fallback(company: str, page, timeout_ms: int) -> PlaywrightResult:
    """Type a configured search term and retry extraction, sniffing network traffic.

    Only called when the initial page load yields zero job rows.
    """
    cfg = load_settings()
    search_term = cfg.get("playwright.search_fallback.search_term", "Data Engineer")
    max_wait_ms = int(cfg.get("playwright.search_fallback.max_wait_ms", 6000))

    found = _find_search_input(page)
    if found is None:
        log.debug("%s: no search input found for fallback", company)
        return PlaywrightResult()

    frame, locator = found

    seen_urls: list[str] = []

    def _record_response(response) -> None:
        seen_urls.append(response.url)

    page.on("response", _record_response)
    try:
        _dismiss_cookie_banner(page)
        submitted = _submit_search(page, frame, locator, search_term)
        if not submitted:
            return PlaywrightResult()

        try:
            page.wait_for_load_state("networkidle", timeout=min(max_wait_ms, timeout_ms))
        except Exception:
            pass
        page.wait_for_timeout(max_wait_ms)
    finally:
        page.remove_listener("response", _record_response)

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

    _click_load_more(page, int(cfg.get("playwright.max_pages", 5)), timeout_ms)
    jobs = _extract_job_rows(page)

    if jobs or discovered_provider:
        log.info(
            "%s: search fallback ('%s') found %s jobs%s",
            company, search_term, len(jobs),
            f", discovered ATS={discovered_provider}" if discovered_provider else "",
        )

    return PlaywrightResult(
        jobs=jobs, discovered_ats_url=discovered_url, discovered_provider=discovered_provider,
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
    user_agent = cfg.get("requests.user_agent")
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

        clicks = _click_load_more(page, max_pages, timeout_ms)
        if clicks:
            log.debug("%s: expanded results with %s pagination action(s)", company, clicks)

        jobs = _extract_job_rows(page)
        if not jobs:
            # JSON-LD is often present even when the visible list is
            # client-rendered, and it carries real posting dates.
            jobs = _extract_jsonld_rows(page)

        log.debug("%s: Playwright extracted %s job rows", company, len(jobs))

        if jobs:
            return PlaywrightResult(jobs=jobs)

        # Nothing on the landing page: try the search box here first (cheap,
        # no navigation), then hop to a dedicated job-list page.
        if search_enabled:
            result = _search_fallback(company, page, timeout_ms)
            if result.jobs or result.discovered_provider:
                return result

        hopped = _navigate_to_job_list(company, page, timeout_ms)
        if hopped.jobs or hopped.discovered_provider:
            return hopped

        # The hop may have landed on a search-driven page; try searching there.
        if search_enabled:
            return _search_fallback(company, page, timeout_ms)

        return PlaywrightResult()

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

    Raises:
        RuntimeError: navigation failed on every attempt.
    """
    cfg = load_settings()
    attempts = max(1, int(cfg.get("playwright.nav_retries", 3)))
    backoff_ms = int(cfg.get("playwright.nav_retry_backoff_ms", 2000))

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return _scrape_once(company, url, attempt)
        except RuntimeError as exc:
            last_error = exc
            if attempt < attempts - 1:
                delay = (backoff_ms * (attempt + 1) + random.randint(0, 750)) / 1000
                log.debug("%s: navigation attempt %s failed (%s); retrying in %.1fs",
                          company, attempt + 1, str(exc)[:90], delay)
                time.sleep(delay)

    raise last_error if last_error else RuntimeError(f"Navigation failed for {url}")
