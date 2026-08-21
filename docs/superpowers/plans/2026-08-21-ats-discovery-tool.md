# ATS Discovery Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An on-demand tool that finds a working ATS URL or job-search page for companies the pipeline cannot currently reach, writing back only URLs it has proven return jobs, and recording `NOT FOUND` for everything else.

**Architecture:** A new pure-logic module `ats/discovery.py` crawls a company's seed URL and root domain over HTTP, escalates to the existing Playwright traversal only when HTTP finds nothing, and verifies every candidate by driving it through a real collector. A thin CLI `tools/find_ats_urls.py` runs it over the workbook and writes suggestions into new columns via `export_ats_urls.py`.

**Tech Stack:** Python 3.12, requests + tenacity (via `http_client`), BeautifulSoup/lxml, Playwright (sync), openpyxl, pandas, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-ats-discovery-tool-design.md`

## Global Constraints

- Always run Python via `.\venv\Scripts\python.exe`, never bare `python`.
- **Never write a URL that has not been verified.** A candidate ATS URL must return **≥ 1 job** through its real collector. A candidate jobs page must render **≥ `playwright.hop_good_enough_rows`** rows (default 10). Anything else is recorded `NOT FOUND`.
- `NOT FOUND` is a first-class successful outcome, not an error. The user resolves those by hand; a guess is worse than a blank.
- Never overwrite the user's hand-curated `ATS URL` or `Live Jobs Page` values by default. Suggestions go to new columns. Two exceptions only: a **blank** `ATS URL` cell may be filled (existing trusted behavior), and `--apply` may overwrite rows whose `Data Retrieved` is `FALSE`.
- Back up the workbook to `companies.xlsx.bak-{timestamp}` before any write, matching `export_ats_urls.py`.
- Per-company isolation: a crash, timeout or malformed page yields `method="none"` with the reason in `note` and the run continues. `discover()` never raises.
- This tool must not touch `data/jobs.db` or `output/company_jobs*.csv`.
- Playwright's sync API is thread-affine; each worker closes its own browser. Never tear down another thread's browser.
- Never run this tool while a full pipeline run is in progress — both write `config/companies.xlsx`.
- The workbook currently has 161 rows and columns: `Company`, `ATS URL`, `Live Jobs Page (if ATS URL unavailable)`, `Data Retrieved`, `Jobs Found`.

---

### Task 1: Detector recognises embedded Oracle Cloud hosts

**Files:**
- Modify: `ats/detector.py` (the `_EMBEDDED_URL_PATTERNS` constant, `TALEO` entry)
- Test: `tests/test_detector_oracle.py`

**Interfaces:**
- Consumes: `extract_any_embedded_ats_url(html_text, provider) -> str | None` and `extract_embedded_ats_url` (both exist).
- Produces: no signature change. `extract_any_embedded_ats_url(html, "taleo")` now returns `fa-*.oraclecloud.com` hosts.

**Why this is Task 1:** it is independent of the tool, verified to work, and recovers a company on its own. `jobs.nokia.com/en/sites/CX_1/jobs` embeds `https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com:443/hcmRestApi/...`, whose Oracle Cloud API returns **575 jobs** (verified 2026-08-21). `HOST_PATTERNS` already maps `oraclecloud.com` to `taleo` and `TaleoCollector._is_oracle_cloud()` already handles it — only the extraction pattern is missing, so the host is never surfaced.

- [ ] **Step 1: Write the failing test**

Create `tests/test_detector_oracle.py`:

```python
from ats.detector import detect_ats, extract_any_embedded_ats_url

# Real markup shape captured from jobs.nokia.com/en/sites/CX_1/jobs.
ORACLE_CX_HTML = (
    '<link rel="icon" href="https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com:443'
    '/hcmRestApi/CandidateExperience/siteFavicon/favicon-16x16.png?siteNumber=CX_1&size=16x16">'
)


def test_embedded_oracle_cloud_host_is_extracted():
    found = extract_any_embedded_ats_url(ORACLE_CX_HTML, "taleo")
    assert found is not None
    assert "fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com" in found


def test_extracted_oracle_host_detects_as_taleo():
    found = extract_any_embedded_ats_url(ORACLE_CX_HTML, "taleo")
    assert detect_ats(found)["provider"] == "taleo"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\venv\Scripts\python.exe -m pytest tests/test_detector_oracle.py -v`
Expected: FAIL — `assert None is not None`.

- [ ] **Step 3: Add oraclecloud.com to the Taleo pattern**

In `ats/detector.py`, find the `TALEO` line inside `_EMBEDDED_URL_PATTERNS` and replace it:

```python
    # Branded Oracle Cloud Recruiting sites (jobs.nokia.com/en/sites/CX_1/jobs)
    # never mention taleo.net; they embed their API host instead. Extracting
    # that host is what lets TaleoCollector's Oracle Cloud path drive them -
    # confirmed against Nokia, which returns 575 jobs this way.
    TALEO: r"https?://[\w.-]*(?:\.taleo\.net|oraclecloud\.com)(?::\d+)?/[^\s\"'<>\\]*",
```

Note the optional `(?::\d+)?` — the embedded URLs carry an explicit `:443` port.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\venv\Scripts\python.exe -m pytest tests/ -q`
Expected: all pass (24 tests: 22 existing + 2 new).

- [ ] **Step 5: Verify against the live company**

Run: `.\venv\Scripts\python.exe main.py --test-company "Nokia"`
Expected: a non-zero "Jobs found" in the diagnostics block, where the previous run reported `NoJobsFound`. If it still fails, run `.\venv\Scripts\python.exe tools/probe_site.py "https://www.nokia.com/about-us/careers/"` and read what the page actually contains before changing anything.

- [ ] **Step 6: Commit**

```bash
git add ats/detector.py tests/test_detector_oracle.py
git commit -m "Extract embedded Oracle Cloud hosts from branded career sites"
```

---

### Task 2: Discovery result type and verification

**Files:**
- Create: `ats/discovery.py`
- Test: `tests/test_discovery_verify.py`

**Interfaces:**
- Consumes: `ats.router.COLLECTORS: dict[str, type[ATSCollector]]`; `ATSCollector(company: str, detection: dict)` then `.collect() -> list[dict]`; `ats.base.CollectorUnavailable`; `ats.detector.detect_ats(url) -> dict`.
- Produces:
  - `@dataclass Discovery` with fields `company: str`, `ats_url: str | None`, `provider: str | None`, `jobs_page: str | None`, `jobs_found: int`, `method: str`, `note: str`.
  - `NOT_FOUND: str = "NOT FOUND"` module constant.
  - `verify_ats_url(company: str, url: str) -> tuple[int, str]` returning `(jobs_found, note)`; `jobs_found == 0` means rejected.

**Why verification is its own task:** it is the rule the whole tool exists to enforce, it is pure and fully testable without network or browser, and a reviewer could reject it independently of any crawling.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_verify.py`:

```python
import pytest

from ats.base import CollectorUnavailable
from ats.discovery import NOT_FOUND, Discovery, verify_ats_url


def test_not_found_constant():
    assert NOT_FOUND == "NOT FOUND"


def test_discovery_defaults_to_nothing_found():
    d = Discovery(company="Acme")
    assert d.ats_url is None
    assert d.jobs_page is None
    assert d.jobs_found == 0
    assert d.method == "none"


def test_verify_accepts_a_url_that_returns_jobs(monkeypatch):
    import ats.discovery as discovery

    class FakeCollector:
        provider = "greenhouse"

        def __init__(self, company, detection):
            pass

        def collect(self):
            return [{"title": "Data Engineer"}, {"title": "Analytics Engineer"}]

    monkeypatch.setitem(discovery.COLLECTORS, "greenhouse", FakeCollector)
    found, note = verify_ats_url("Acme", "https://boards.greenhouse.io/acme")
    assert found == 2
    assert "2 jobs" in note


def test_verify_rejects_a_url_that_returns_no_jobs(monkeypatch):
    import ats.discovery as discovery

    class EmptyCollector:
        provider = "greenhouse"

        def __init__(self, company, detection):
            pass

        def collect(self):
            return []

    monkeypatch.setitem(discovery.COLLECTORS, "greenhouse", EmptyCollector)
    found, note = verify_ats_url("Acme", "https://boards.greenhouse.io/acme")
    assert found == 0
    assert "zero jobs" in note


def test_verify_rejects_when_the_collector_raises(monkeypatch):
    import ats.discovery as discovery

    class BrokenCollector:
        provider = "greenhouse"

        def __init__(self, company, detection):
            pass

        def collect(self):
            raise CollectorUnavailable("board not found")

    monkeypatch.setitem(discovery.COLLECTORS, "greenhouse", BrokenCollector)
    found, note = verify_ats_url("Acme", "https://boards.greenhouse.io/acme")
    assert found == 0
    assert "board not found" in note


def test_verify_rejects_an_unrecognised_url():
    found, note = verify_ats_url("Acme", "https://www.acme.com/careers/")
    assert found == 0
    assert "no collector" in note.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\venv\Scripts\python.exe -m pytest tests/test_discovery_verify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ats.discovery'`.

- [ ] **Step 3: Create `ats/discovery.py` with the type and verifier**

```python
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

from ats.detector import UNKNOWN, detect_ats
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\venv\Scripts\python.exe -m pytest tests/ -q`
Expected: all pass (30 tests: 24 + 6 new).

- [ ] **Step 5: Commit**

```bash
git add ats/discovery.py tests/test_discovery_verify.py
git commit -m "Add Discovery result type and collector-backed verification"
```

---

### Task 3: HTTP candidate crawl

**Files:**
- Modify: `ats/discovery.py`
- Test: `tests/test_discovery_candidates.py`

**Interfaces:**
- Consumes: `Discovery`, `verify_ats_url` (Task 2); `http_client.get_text(url, **kwargs) -> str`; `ats.detector.detect_from_html(html, final_url=...) -> str`, `extract_any_embedded_ats_url(html, provider) -> str | None`, `detect_ats`; `ats.html_utils.make_soup(html) -> BeautifulSoup`; `browser.playwright_scraper.JOBS_PAGE_HREF_HINTS: tuple[str, ...]`.
- Produces:
  - `root_domain_url(url: str) -> str | None` — `https://careers.frostbank.com/us/en` → `https://frostbank.com`.
  - `candidates_from_html(html: str, base_url: str) -> list[str]` — ATS URLs found in a page, most specific first.
  - `careers_links(html: str, base_url: str, limit: int = 5) -> list[str]` — careers-ish links to crawl one level deeper.

**Why the root domain matters:** a marketing careers page often does not link to the ATS, while the corporate homepage footer does. Seeding both is what makes the HTTP stage worth running at all.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_candidates.py`:

```python
from ats.discovery import candidates_from_html, careers_links, root_domain_url

ORACLE_PAGE = (
    '<html><body>'
    '<link href="https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com:443'
    '/hcmRestApi/CandidateExperience/siteFavicon/favicon-16x16.png?siteNumber=CX_1">'
    '</body></html>'
)

MARKETING_PAGE = (
    '<html><body>'
    '<a href="/about">About us</a>'
    '<a href="/careers/search">Search jobs</a>'
    '<a href="/en/openings">Current openings</a>'
    '<a href="https://twitter.com/acme">Twitter</a>'
    '</body></html>'
)

NOTHING_PAGE = '<html><body><p>We are hiring soon.</p></body></html>'


def test_root_domain_strips_subdomain_and_path():
    assert root_domain_url("https://careers.frostbank.com/us/en") == "https://frostbank.com"


def test_root_domain_handles_bare_host():
    assert root_domain_url("https://acme.com") == "https://acme.com"


def test_root_domain_returns_none_for_garbage():
    assert root_domain_url("not a url") is None


def test_candidates_finds_embedded_oracle_host():
    found = candidates_from_html(ORACLE_PAGE, "https://jobs.nokia.com/en/sites/CX_1/jobs")
    assert any("oraclecloud.com" in c for c in found)


def test_candidates_empty_when_page_names_no_ats():
    assert candidates_from_html(NOTHING_PAGE, "https://acme.com/careers") == []


def test_careers_links_prefers_job_list_hrefs():
    links = careers_links(MARKETING_PAGE, "https://acme.com/careers")
    assert "https://acme.com/careers/search" in links
    assert "https://acme.com/en/openings" in links


def test_careers_links_ignores_unrelated_links():
    links = careers_links(MARKETING_PAGE, "https://acme.com/careers")
    assert not any("twitter.com" in link for link in links)
    assert not any(link.endswith("/about") for link in links)


def test_careers_links_respects_limit():
    assert len(careers_links(MARKETING_PAGE, "https://acme.com/careers", limit=1)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\venv\Scripts\python.exe -m pytest tests/test_discovery_candidates.py -v`
Expected: FAIL — `ImportError: cannot import name 'candidates_from_html'`.

- [ ] **Step 3: Implement the three helpers**

Append to `ats/discovery.py`, and add these imports at the top of the file:

```python
from urllib.parse import urljoin, urlsplit

import http_client
from ats.detector import (
    SUPPORTED_PROVIDERS,
    detect_from_html,
    extract_any_embedded_ats_url,
)
from ats.html_utils import make_soup
from browser.playwright_scraper import JOBS_PAGE_HREF_HINTS
```

```python
def root_domain_url(url: str) -> str | None:
    """Corporate homepage for a careers URL.

    ``https://careers.frostbank.com/us/en`` -> ``https://frostbank.com``. A
    marketing careers page often does not link to the ATS while the corporate
    footer does, so both are worth crawling.
    """
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\venv\Scripts\python.exe -m pytest tests/ -q`
Expected: all pass (38 tests: 30 + 8 new).

- [ ] **Step 5: Commit**

```bash
git add ats/discovery.py tests/test_discovery_candidates.py
git commit -m "Add HTTP candidate extraction for ATS discovery"
```

---

### Task 4: The `discover()` entry point

**Files:**
- Modify: `ats/discovery.py`
- Test: `tests/test_discovery_discover.py`

**Interfaces:**
- Consumes: everything from Tasks 2 and 3; `browser.playwright_scraper._navigate_to_job_list(company, page, timeout_ms, max_hops=None) -> PlaywrightResult` with fields `.jobs`, `.discovered_ats_url`, `.discovered_provider`; `browser.playwright_scraper._extract_job_rows(page) -> list[dict]`; `settings.load_settings()`.
- Produces: `discover(company: str, seed_url: str | None, *, use_browser: bool = True) -> Discovery`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery_discover.py`. These tests stub the network so they are fast and deterministic:

```python
import ats.discovery as discovery
from ats.discovery import discover


def test_returns_not_found_when_there_is_no_seed():
    result = discover("Acme", None, use_browser=False)
    assert result.company == "Acme"
    assert result.ats_url is None
    assert result.jobs_found == 0
    assert result.method == "none"


def test_http_stage_finds_and_verifies_an_ats(monkeypatch):
    page = (
        '<link href="https://fa-x.fa.ocs.oraclecloud.com:443/hcmRestApi/'
        'CandidateExperience/siteFavicon/favicon-16x16.png?siteNumber=CX_1">'
    )
    monkeypatch.setattr(discovery, "_fetch", lambda url: page)
    monkeypatch.setattr(discovery, "verify_ats_url", lambda c, u: (575, "taleo API returned 575 jobs"))

    result = discover("Nokia", "https://jobs.nokia.com/en/sites/CX_1/jobs", use_browser=False)
    assert result.jobs_found == 575
    assert result.ats_url is not None
    assert "oraclecloud.com" in result.ats_url
    assert result.method == "http"


def test_unverifiable_candidate_is_not_written(monkeypatch):
    page = '<link href="https://fa-x.fa.ocs.oraclecloud.com:443/hcmRestApi/x?siteNumber=CX_1">'
    monkeypatch.setattr(discovery, "_fetch", lambda url: page)
    monkeypatch.setattr(discovery, "verify_ats_url", lambda c, u: (0, "taleo collector returned zero jobs"))

    result = discover("Nokia", "https://jobs.nokia.com/en/sites/CX_1/jobs", use_browser=False)
    assert result.ats_url is None
    assert result.jobs_found == 0
    assert result.method == "none"
    assert "zero jobs" in result.note


def test_fetch_failure_is_contained(monkeypatch):
    def boom(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(discovery, "_fetch", boom)
    result = discover("Acme", "https://acme.com/careers", use_browser=False)
    assert result.method == "none"
    assert result.jobs_found == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\venv\Scripts\python.exe -m pytest tests/test_discovery_discover.py -v`
Expected: FAIL — `ImportError: cannot import name 'discover'`.

- [ ] **Step 3: Implement `_fetch` and `discover`**

Append to `ats/discovery.py`:

```python
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

    if use_browser:
        browser_result = _discover_via_browser(company, seeds[0])
        if browser_result.jobs_found:
            return browser_result
        last_note = browser_result.note or last_note

    result.note = last_note
    return result
```

- [ ] **Step 4: Implement the browser stage**

Still in `ats/discovery.py`. Playwright is imported lazily so an HTTP-only sweep never pays for it:

```python
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
        page.goto(seed_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(int(cfg.get("playwright.wait_after_load_ms", 2500)))
        _dismiss_cookie_banner(page)

        traversal = _navigate_to_job_list(company, page, timeout_ms, max_hops=6)

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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\venv\Scripts\python.exe -m pytest tests/ -q`
Expected: all pass (42 tests: 38 + 4 new).

- [ ] **Step 6: Verify against a live company**

Run:

```bash
.\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from ats.discovery import discover; print(discover('Nokia','https://jobs.nokia.com/en/sites/CX_1/jobs',use_browser=False))"
```

Expected: a `Discovery` with `method='http'`, a `fa-*.oraclecloud.com` `ats_url`, and `jobs_found` around 575.

- [ ] **Step 7: Commit**

```bash
git add ats/discovery.py tests/test_discovery_discover.py
git commit -m "Add discover(): HTTP crawl with browser escalation, verified"
```

---

### Task 5: Suggestion write-back

**Files:**
- Modify: `export_ats_urls.py`
- Test: `tests/test_write_suggestions.py`

**Interfaces:**
- Consumes: `Discovery` and `NOT_FOUND` (Task 2); existing `_blank`, `_backup_path` helpers in `export_ats_urls.py`.
- Produces: `write_suggestions(companies_path: Path | str, discoveries: list[Discovery], *, apply: bool = False) -> dict[str, Any]` returning `{"updated": int, "applied": int, "backup_path": Path | None}`.

**Columns written:** `Suggested ATS URL`, `Suggested Jobs Page`, `Discovery Notes`. Created at the end of the header row if absent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_write_suggestions.py`:

```python
import openpyxl
import pytest

from ats.discovery import Discovery
from export_ats_urls import write_suggestions

HEADERS = ["Company", "ATS URL", "Live Jobs Page (if ATS URL unavailable)",
           "Data Retrieved", "Jobs Found"]


@pytest.fixture
def workbook(tmp_path):
    path = tmp_path / "companies.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    ws.append(["Nokia", None, "https://www.nokia.com/about-us/careers/", "FALSE", 0])
    ws.append(["Infosys", "https://www.infosys.com/careers/",
               "https://www.infosys.com/careers/", "FALSE", 0])
    ws.append(["Capital One", "https://capitalone.wd12.myworkdayjobs.com/Capital_One",
               None, "TRUE", 500])
    wb.save(path)
    return path


def _read(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        record = dict(zip(headers, row))
        rows[record["Company"]] = record
    wb.close()
    return headers, rows


def test_creates_the_three_suggestion_columns(workbook):
    write_suggestions(workbook, [Discovery(company="Nokia", ats_url="https://fa-x.oraclecloud.com",
                                           provider="taleo", jobs_found=575,
                                           method="http", note="taleo API returned 575 jobs")])
    headers, _ = _read(workbook)
    assert "Suggested ATS URL" in headers
    assert "Suggested Jobs Page" in headers
    assert "Discovery Notes" in headers


def test_verified_finding_is_suggested(workbook):
    write_suggestions(workbook, [Discovery(company="Nokia", ats_url="https://fa-x.oraclecloud.com",
                                           provider="taleo", jobs_found=575,
                                           method="http", note="taleo API returned 575 jobs")])
    _, rows = _read(workbook)
    assert rows["Nokia"]["Suggested ATS URL"] == "https://fa-x.oraclecloud.com"
    assert "575" in rows["Nokia"]["Discovery Notes"]


def test_nothing_found_is_recorded_as_not_found(workbook):
    write_suggestions(workbook, [Discovery(company="Infosys", note="nothing found in page HTML")])
    _, rows = _read(workbook)
    assert rows["Infosys"]["Suggested ATS URL"] == "NOT FOUND"
    assert rows["Infosys"]["Suggested Jobs Page"] == "NOT FOUND"


def test_curated_values_are_never_overwritten_without_apply(workbook):
    write_suggestions(workbook, [Discovery(company="Infosys", ats_url="https://boards.greenhouse.io/infy",
                                           provider="greenhouse", jobs_found=12,
                                           method="http", note="greenhouse API returned 12 jobs")])
    _, rows = _read(workbook)
    assert rows["Infosys"]["ATS URL"] == "https://www.infosys.com/careers/"


def test_blank_ats_cell_is_filled_directly(workbook):
    write_suggestions(workbook, [Discovery(company="Nokia", ats_url="https://fa-x.oraclecloud.com",
                                           provider="taleo", jobs_found=575,
                                           method="http", note="ok")])
    _, rows = _read(workbook)
    assert rows["Nokia"]["ATS URL"] == "https://fa-x.oraclecloud.com"


def test_apply_overwrites_only_failing_rows(workbook):
    write_suggestions(
        workbook,
        [
            Discovery(company="Infosys", ats_url="https://boards.greenhouse.io/infy",
                      provider="greenhouse", jobs_found=12, method="http", note="ok"),
            Discovery(company="Capital One", ats_url="https://example.com/wrong",
                      provider="workday", jobs_found=1, method="http", note="ok"),
        ],
        apply=True,
    )
    _, rows = _read(workbook)
    assert rows["Infosys"]["ATS URL"] == "https://boards.greenhouse.io/infy"
    # Capital One is Data Retrieved = TRUE, so --apply must leave it alone.
    assert rows["Capital One"]["ATS URL"] == "https://capitalone.wd12.myworkdayjobs.com/Capital_One"


def test_a_backup_is_created(workbook):
    result = write_suggestions(workbook, [Discovery(company="Nokia", note="nothing")])
    assert result["backup_path"] is not None
    assert result["backup_path"].exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\venv\Scripts\python.exe -m pytest tests/test_write_suggestions.py -v`
Expected: FAIL — `ImportError: cannot import name 'write_suggestions'`.

- [ ] **Step 3: Implement `write_suggestions`**

Append to `export_ats_urls.py`, adding `from ats.discovery import NOT_FOUND, Discovery` at the top:

```python
def write_suggestions(
    companies_path: Path | str,
    discoveries: list[Discovery],
    *,
    apply: bool = False,
    company_column: str = "Company",
    ats_url_column: str = "ATS URL",
    jobs_page_column: str = "Live Jobs Page (if ATS URL unavailable)",
    status_column: str = "Data Retrieved",
) -> dict[str, Any]:
    """Record discovery results without destroying hand-curated values.

    Suggestions go to three new columns by default. The workbook's own
    ``ATS URL`` / ``Live Jobs Page`` values were curated by hand, and silently
    overwriting them would be hostile - a wrong overwrite is far harder to
    notice than an extra column.

    Two exceptions write the real columns:

    * a **blank** ``ATS URL`` cell is filled with a verified URL, matching
      :func:`write_discovered_urls`' established behaviour;
    * ``apply=True`` promotes suggestions, but only for rows whose
      ``Data Retrieved`` is ``FALSE`` - a value already failing cannot be made
      worse by a verified one.

    Returns ``{"updated": int, "applied": int, "backup_path": Path | None}``.
    """
    path = Path(companies_path)
    if not discoveries or not path.exists():
        return {"updated": 0, "applied": 0, "backup_path": None}

    by_company = {d.company: d for d in discoveries}

    try:
        workbook = load_workbook(path)
        sheet = workbook.active

        header_row = next(sheet.iter_rows(min_row=1, max_row=1))
        headers = {str(cell.value).strip(): cell.column for cell in header_row if cell.value}

        company_col = headers.get(company_column)
        if not company_col:
            log.warning("Workbook %s missing %s column; skipping suggestions",
                        path.name, company_column)
            return {"updated": 0, "applied": 0, "backup_path": None}

        def _column(name: str) -> int:
            existing = headers.get(name)
            if existing:
                return existing
            index = sheet.max_column + 1
            sheet.cell(row=1, column=index, value=name)
            headers[name] = index
            return index

        suggested_ats_col = _column("Suggested ATS URL")
        suggested_page_col = _column("Suggested Jobs Page")
        notes_col = _column("Discovery Notes")

        ats_col = headers.get(ats_url_column)
        page_col = headers.get(jobs_page_column)
        status_col = headers.get(status_column)

        updated = applied = 0
        for row in sheet.iter_rows(min_row=2):
            company_cell = row[company_col - 1]
            name = str(company_cell.value).strip() if company_cell.value else ""
            found = by_company.get(name)
            if not name or found is None:
                continue

            line = company_cell.row
            sheet.cell(row=line, column=suggested_ats_col,
                       value=found.ats_url or NOT_FOUND)
            sheet.cell(row=line, column=suggested_page_col,
                       value=found.jobs_page or NOT_FOUND)
            sheet.cell(row=line, column=notes_col,
                       value=f"{found.method}: {found.note}" if found.note else found.method)
            updated += 1

            if not found.jobs_found:
                continue

            # Exception 1: fill a blank ATS URL cell.
            if found.ats_url and ats_col and _blank(row[ats_col - 1].value):
                row[ats_col - 1].value = found.ats_url
                applied += 1
                continue

            if not apply:
                continue

            # Exception 2: --apply, restricted to rows already failing.
            failing = status_col and str(row[status_col - 1].value).strip().upper() == "FALSE"
            if not failing:
                continue
            if found.ats_url and ats_col:
                row[ats_col - 1].value = found.ats_url
                applied += 1
            elif found.jobs_page and page_col:
                row[page_col - 1].value = found.jobs_page
                applied += 1

        backup_path = _backup_path(path)
        shutil.copy2(path, backup_path)
        workbook.save(path)
        log.info("Wrote %s suggestion row(s), applied %s, to %s (backup: %s)",
                 updated, applied, path.name, backup_path.name)
        return {"updated": updated, "applied": applied, "backup_path": backup_path}

    except Exception as exc:
        log.warning("Could not write suggestions to %s: %s", path, exc)
        return {"updated": 0, "applied": 0, "backup_path": None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\venv\Scripts\python.exe -m pytest tests/ -q`
Expected: all pass (49 tests: 42 + 7 new).

- [ ] **Step 5: Commit**

```bash
git add export_ats_urls.py tests/test_write_suggestions.py
git commit -m "Write discovery suggestions without overwriting curated values"
```

---

### Task 6: The CLI

**Files:**
- Create: `tools/find_ats_urls.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `discover()` (Task 4), `write_suggestions()` (Task 5), `pipeline.resolve_companies_path(settings, excel_path) -> Path`, `pipeline.load_companies(settings, excel_path) -> pd.DataFrame`, `settings.load_settings()`, `logger.setup_logging(path, level, quiet=...)`.
- Produces: CLI `tools/find_ats_urls.py` writing `output/ats_discovery.csv`.

- [ ] **Step 1: Write the CLI**

Create `tools/find_ats_urls.py`:

```python
"""Find a working ATS URL or job-search page for companies we cannot reach.

Not part of the pipeline - an on-demand tool. Every URL it records has been
driven through a real collector and returned jobs; anything it cannot prove
is recorded as NOT FOUND for you to resolve by hand.

    python tools/find_ats_urls.py                 # every row without a verified path
    python tools/find_ats_urls.py --only-failures  # only Data Retrieved = FALSE
    python tools/find_ats_urls.py --no-browser     # HTTP stage only, much faster
    python tools/find_ats_urls.py --limit 10
    python tools/find_ats_urls.py --apply          # promote suggestions into the real columns

Never run this while a full pipeline run is in progress: both write
config/companies.xlsx.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats.discovery import discover  # noqa: E402
from export_ats_urls import write_suggestions  # noqa: E402
from logger import setup_logging  # noqa: E402
from pipeline import load_companies, resolve_companies_path  # noqa: E402
from settings import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-failures", action="store_true",
                        help="only rows whose Data Retrieved is FALSE")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true",
                        help="HTTP stage only; much faster, finds less")
    parser.add_argument("--apply", action="store_true",
                        help="promote verified suggestions into ATS URL / Live Jobs Page")
    args = parser.parse_args()

    setup_logging("logs/discovery.log", "INFO", quiet=True)
    cfg = load_settings()
    companies = load_companies(cfg)

    columns = cfg.get("columns", {})
    name_col = columns.get("company", "Company")
    ats_col = columns.get("ats_url", "ATS URL")
    live_col = columns.get("live_jobs_url", "Live Jobs Page (if ATS URL unavailable)")

    def _blank(value) -> bool:
        return value is None or str(value).strip().lower() in {"", "nan", "none"}

    rows = []
    seen: set[str] = set()
    for _, record in companies.iterrows():
        name = str(record.get(name_col) or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)

        if args.only_failures:
            status = str(record.get("Data Retrieved") or "").strip().upper()
            if status != "FALSE":
                continue

        seed = record.get(ats_col)
        if _blank(seed):
            seed = record.get(live_col)
        rows.append((name, None if _blank(seed) else str(seed).strip()))

    if args.limit:
        rows = rows[: args.limit]

    print(f"Discovering for {len(rows)} companies "
          f"({'HTTP only' if args.no_browser else 'HTTP + browser'})\n")

    results = []
    for name, seed in rows:
        found = discover(name, seed, use_browser=not args.no_browser)
        results.append(found)
        marker = "OK  " if found.jobs_found else "----"
        target = found.ats_url or found.jobs_page or "NOT FOUND"
        print(f"  {marker}  {name[:34]:<34} {found.method:<8} "
              f"{found.jobs_found:>5}  {target[:58]}")

    out_path = Path("output/ats_discovery.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "ats_url", "provider", "jobs_page",
                         "jobs_found", "method", "note"])
        for found in results:
            writer.writerow([found.company, found.ats_url or "", found.provider or "",
                             found.jobs_page or "", found.jobs_found, found.method, found.note])

    verified = sum(1 for r in results if r.jobs_found)
    print(f"\nVerified {verified} of {len(results)}; wrote {out_path}")

    export = write_suggestions(
        resolve_companies_path(cfg), results, apply=args.apply
    )
    print(f"Workbook: {export['updated']} suggestion row(s), "
          f"{export['applied']} applied"
          + (f", backup {export['backup_path'].name}" if export["backup_path"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test on a single known company**

Confirm no pipeline run is active first (`ps aux | grep -i python` must show none) and that `config/companies.xlsx` is closed in Excel (no `config/~$companies.xlsx`).

Run: `.\venv\Scripts\python.exe tools/find_ats_urls.py --only-failures --no-browser --limit 3`
Expected: three result lines, `output/ats_discovery.csv` written, and a workbook line reporting suggestion rows. Verify by opening the workbook that `Suggested ATS URL` contains either a real URL or `NOT FOUND`, and that no pre-existing `ATS URL` value changed.

- [ ] **Step 3: Run the full sweep over the failing companies**

Run: `.\venv\Scripts\python.exe tools/find_ats_urls.py --only-failures`
Expected: Nokia verified via the Oracle Cloud host (~575 jobs). Note the wall-clock; if it exceeds ~20 minutes, rerun with `--no-browser` for a faster pass and record the difference.

- [ ] **Step 4: Confirm no regression to the normal pipeline**

Run: `.\venv\Scripts\python.exe tools/canary.py`
Expected: `CANARY PASSED: all 9 collection paths returned jobs`. The only shared change is Task 1's additive detector pattern.

- [ ] **Step 5: Document the tool in the README**

In `README.md`, under the "Before trusting a full run" section, add:

````markdown
## Finding missing ATS URLs

`tools/find_ats_urls.py` crawls a company's careers page and corporate
homepage looking for a real ATS, verifies every candidate by driving it
through the actual collector, and records what it proves:

```bash
python tools/find_ats_urls.py --only-failures
```

Verified findings land in `Suggested ATS URL` / `Suggested Jobs Page`, with a
`Discovery Notes` column explaining each. Anything it cannot verify is written
as `NOT FOUND` — deliberately, so those can be fixed by hand rather than
filled with a guess. Your existing `ATS URL` and `Live Jobs Page` values are
never overwritten unless you pass `--apply` (and even then, only for rows
already marked `Data Retrieved = FALSE`). Blank `ATS URL` cells are filled
directly, as they already are during a normal run.

Do not run it while a full run is in progress — both write the workbook.
````

- [ ] **Step 6: Commit**

```bash
git add tools/find_ats_urls.py README.md
git commit -m "Add tools/find_ats_urls.py: verified ATS discovery sweep"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `ats/discovery.py` with `Discovery` dataclass | 2 |
| Seeding from sheet URL + root domain | 3 (`root_domain_url`), 4 (`_seeds`) |
| Stage 1 HTTP crawl, one level deeper via `JOBS_PAGE_HREF_HINTS` | 3, 4 |
| Stage 2 browser escalation only on miss, larger budget | 4 (`_discover_via_browser`, `max_hops=6`) |
| Verify ATS ≥ 1 job via real collector | 2 (`verify_ats_url`) |
| Verify jobs page ≥ `hop_good_enough_rows` | 4 (`_discover_via_browser`) |
| `NOT FOUND` as first-class outcome | 2 (constant), 5 (written), 6 (reported) |
| Suggest-only write-back, 3 new columns | 5 |
| Blank `ATS URL` filled directly | 5 |
| `--apply` limited to `Data Retrieved = FALSE` | 5 |
| Backup before write | 5 |
| Per-company isolation, never raises | 4 |
| `tools/find_ats_urls.py` with the documented flags | 6 |
| `output/ats_discovery.csv` | 6 |
| Oracle Cloud embedded-host detector fix | 1 |
| Testing: unit, write-back, live smoke, canary regression | 1-6 |

No gaps. `selectminds.com` is deliberately unimplemented — the spec makes no claim it is recoverable, and Task 6's sweep will verify it or report `NOT FOUND`. Jobvite and Njoyn collectors are out of scope per the spec.

**Placeholder scan:** none. Every step carries runnable code or an exact command.

**Type consistency:** `Discovery` field names (`ats_url`, `provider`, `jobs_page`, `jobs_found`, `method`, `note`) are identical in Tasks 2, 4, 5 and 6. `verify_ats_url` returns `(int, str)` in Task 2 and is consumed that way in Task 4. `write_suggestions` returns `{"updated", "applied", "backup_path"}` in Task 5 and is read with those keys in Task 6. `NOT_FOUND` is defined in Task 2 and imported in Task 5.
