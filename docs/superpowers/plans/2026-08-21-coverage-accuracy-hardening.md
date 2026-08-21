# Coverage & Accuracy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise company coverage from 123/171 (71.9%) toward 90% by making the browser fallback traverse careers sites several layers deep, and eliminate the systematic missing-date gap affecting 5,635 collected jobs.

**Architecture:** Three independent workstreams against the existing pipeline. (1) `browser/playwright_scraper.py`'s `_navigate_to_job_list` becomes a budgeted best-first traversal (currently 2 hops, one candidate per level) so job lists buried behind "Jobs"/"Search Jobs"/"Career Areas" links are found. (2) Date extraction is added to the three collection paths that never populate `date_posted` — the Playwright DOM extractor, the shared HTML-fallback helper, and by extension SuccessFactors/iCIMS/Avature. (3) Targeted provider fixes (Taleo error masking, Workday vanity-host coordinates) plus resilience work (pinned deps, canary smoke test).

**Tech Stack:** Python 3.12, Playwright (sync API), BeautifulSoup/lxml, pandas, openpyxl, requests+tenacity, pytest (added by Task 1).

**Spec:** `docs/superpowers/specs/2026-08-21-scraper-accuracy-speed-resilience-coverage-design.md`

## Global Constraints

- Always run Python via `.\venv\Scripts\python.exe`, never bare `python`.
- Never run two pipeline instances at once — they contend over `data/jobs.db` and `config/companies.xlsx`.
- Playwright's sync API is thread-affine; each worker must close its own browser. Never tear down another thread's browser.
- Partial runs (`--test-company`, `--test-provider`, `--limit`) write `test_`-prefixed outputs and never touch the workbook. Use them for all per-company verification.
- Never invent data. A missing date stays `None` and is flagged `date_unavailable`; a missing title/URL means `build_record` returns `None`. Do not substitute "now" for an unknown date.
- This project is independent of the JobSpy scraper in `..\job-scraper`. Never merge, read, or deduplicate against it.
- A full run takes ~14 minutes. Run it in the background; do not block on it.
- Baseline to beat (run3, 2026-08-21): 123/171 companies, 216 target data jobs, 36 DFW/remote, 18 within-window, 39,947 jobs collected, 13.6 min wall-clock.

---

### Task 1: Test infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_normalize.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: a working `pytest` invocation (`.\venv\Scripts\python.exe -m pytest tests/ -v`) that later tasks add cases to.

- [ ] **Step 1: Install pytest and pin the two drifting dependencies**

Edit `requirements.txt` — change the two floating Playwright lines and add pytest. The versions below are the ones confirmed installed and working on 2026-08-21:

```
playwright==1.62.0
playwright-stealth==2.0.3
pytest==8.3.4
```

Then install:

```bash
./venv/Scripts/python.exe -m pip install -r requirements.txt
```

- [ ] **Step 2: Create the test package**

Create `tests/__init__.py` as an empty file.

- [ ] **Step 3: Write tests for existing `parse_date` behavior**

These lock in behavior that later tasks must not break. Create `tests/test_normalize.py`:

```python
from datetime import datetime, timedelta, timezone

from normalize import build_record, parse_date

REF = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_date_handles_iso():
    assert parse_date("2026-08-18T00:00:00+00:00", reference=REF).day == 18


def test_parse_date_handles_relative_days():
    assert parse_date("Posted 3 Days Ago", reference=REF) == REF - timedelta(days=3)


def test_parse_date_handles_today():
    assert parse_date("Posted Today", reference=REF) == REF


def test_parse_date_returns_none_for_garbage():
    assert parse_date("see description", reference=REF) is None


def test_parse_date_rejects_far_future():
    assert parse_date("2030-01-01", reference=REF) is None


def test_build_record_requires_title_and_url():
    assert build_record(
        company="X", title=None, job_url="https://e.com/j/1",
        ats_provider="workday", scraping_method="direct_api",
    ) is None
    assert build_record(
        company="X", title="Data Engineer", job_url=None,
        ats_provider="workday", scraping_method="direct_api",
    ) is None
```

- [ ] **Step 4: Run the tests**

Run: `./venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/
git commit -m "Add pytest and pin Playwright dependencies"
```

---

### Task 2: Fix the misleading date-window labels

**Files:**
- Modify: `filters.py:17-19`, `filters.py:186-192`
- Modify: `pipeline.py` (wherever `WITHIN_WINDOW`/`OUTSIDE_WINDOW` strings are compared or printed)
- Test: `tests/test_filters.py`

**Interfaces:**
- Consumes: pytest from Task 1.
- Produces: `filters.WITHIN_WINDOW == "within_window"`, `filters.OUTSIDE_WINDOW == "older_than_window"`. `filters.DATE_UNAVAILABLE` is unchanged at `"date_unavailable"`. Later tasks and `pipeline.py` must use these constants, never the literal strings.

**Why:** `config/settings.yaml` now sets `hours_old: 168`, but the status literal is hardcoded `"within_72_hours"`. Every row in `output/company_jobs.csv` currently claims "within_72_hours" when it actually means "within 168 hours" — the output actively misstates the data.

- [ ] **Step 1: Write the failing test**

Create `tests/test_filters.py`:

```python
from datetime import datetime, timedelta, timezone

import filters
from filters import DATE_UNAVAILABLE, OUTSIDE_WINDOW, WITHIN_WINDOW, classify_date

REF = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


def test_status_labels_are_window_agnostic():
    assert WITHIN_WINDOW == "within_window"
    assert OUTSIDE_WINDOW == "older_than_window"
    assert DATE_UNAVAILABLE == "date_unavailable"


def test_classify_date_respects_hours_old():
    record = {"date_posted": (REF - timedelta(hours=100)).isoformat()}
    assert classify_date(record, hours_old=72, now=REF) == OUTSIDE_WINDOW
    assert classify_date(record, hours_old=168, now=REF) == WITHIN_WINDOW


def test_classify_date_without_any_date():
    assert classify_date({}, hours_old=168, now=REF) == DATE_UNAVAILABLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_filters.py -v`
Expected: FAIL — `assert 'within_72_hours' == 'within_window'`.

- [ ] **Step 3: Rename the constants**

In `filters.py`, replace lines 16-19:

```python
# Date filter status values. Deliberately window-agnostic: the actual
# cutoff comes from settings.yaml's hours_old, so baking "72" into the
# label would misstate the data whenever that value changes.
WITHIN_WINDOW = "within_window"
OUTSIDE_WINDOW = "older_than_window"
DATE_UNAVAILABLE = "date_unavailable"
```

- [ ] **Step 4: Update every consumer of the old literals**

Run this to find them:

```bash
grep -rn "within_72_hours\|older_than_72_hours" --include=*.py .
```

Replace each hit with the imported constant (`WITHIN_WINDOW` / `OUTSIDE_WINDOW`). In `pipeline.py`, the run-summary line that prints `Within last 72 hours:` must become dynamic — it reads `hours_old` from settings, so print:

```python
print(f"Within last {hours_old} hours:   {summary.within_window:,}")
```

Also update `README.md`'s Output section, which documents `date_filter_status` as `within_72_hours`/`older_than_72_hours`, to the new values.

- [ ] **Step 5: Run tests**

Run: `./venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 9 passed.

- [ ] **Step 6: Verify against a real company**

Run: `./venv/Scripts/python.exe main.py --test-company "Capital One"`
Expected: summary prints `Within last 168 hours:` and no traceback.

- [ ] **Step 7: Commit**

```bash
git add filters.py pipeline.py README.md tests/test_filters.py
git commit -m "Make date-window status labels reflect configured hours_old"
```

---

### Task 3: Extract posting dates in the Playwright DOM path

**Files:**
- Modify: `browser/playwright_scraper.py` (`_EXTRACT_JS` near line 197, `_extract_job_rows` near line 463)
- Test: `tests/test_playwright_extract.py`

**Interfaces:**
- Consumes: pytest from Task 1.
- Produces: rows from `_extract_job_rows` gain a populated `date_posted` when the DOM exposes one. The dict shape stays `{"title", "location", "job_url", "date_posted"}` — unchanged keys, so `ats/router.py::collect_via_browser` needs no edit.

**Why:** 4,189 of run3's 39,947 collected jobs came from `unknown/playwright` and *every one* has `date_posted = None` — `_extract_job_rows` hardcodes it. These jobs can never pass the freshness filter on their own merits; they survive only via the SQLite `first_seen` fallback. This is the single largest accuracy gap in the pipeline.

- [ ] **Step 1: Write the failing test**

Create `tests/test_playwright_extract.py`. This tests the JS extractor against a real DOM using Playwright's own browser — no network:

```python
import pytest

from browser.playwright_scraper import _extract_job_rows

HTML = """
<html><body>
  <div class="card">
    <a href="/jobs/1">Senior Data Engineer</a>
    <span class="job-location">Dallas, TX</span>
    <time datetime="2026-08-18T00:00:00Z">Aug 18</time>
  </div>
  <div class="card">
    <a href="/jobs/2">Analytics Engineer</a>
    <span class="location">Plano, TX</span>
    <span class="posted-date">3 days ago</span>
  </div>
</body></html>
"""


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    p = browser.new_page()
    p.set_content(HTML)
    yield p
    browser.close()
    pw.stop()


def test_extracts_datetime_attribute(page):
    rows = _extract_job_rows(page)
    row = next(r for r in rows if "Senior Data Engineer" in r["title"])
    assert row["date_posted"] == "2026-08-18T00:00:00Z"


def test_extracts_relative_date_text(page):
    rows = _extract_job_rows(page)
    row = next(r for r in rows if "Analytics Engineer" in r["title"])
    assert row["date_posted"] == "3 days ago"


def test_still_extracts_location(page):
    rows = _extract_job_rows(page)
    row = next(r for r in rows if "Senior Data Engineer" in r["title"])
    assert row["location"] == "Dallas, TX"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_playwright_extract.py -v`
Expected: FAIL — `assert None == '2026-08-18T00:00:00Z'`.

- [ ] **Step 3: Add date extraction to the JS extractor**

In `browser/playwright_scraper.py`, inside `_EXTRACT_JS`, add a `nearbyDate` helper right after the existing `nearbyLocation` function (before the `for (const selector of selectors)` loop):

```javascript
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
```

Then change the push at the end of the selector loop to include the date:

```javascript
      out.push({ title, href, location: nearbyLocation(el), date: nearbyDate(el) });
```

- [ ] **Step 4: Pass the date through in `_extract_job_rows`**

In `_extract_job_rows`, change the appended dict (currently hardcoding `"date_posted": None`) to:

```python
        results.append({
            "title": title,
            "location": _clean_location(row.get("location")),
            "job_url": absolute,
            "date_posted": row.get("date"),
        })
```

`normalize.parse_date` already handles both the ISO strings and the "3 days ago" phrasing this returns, and returns `None` for anything it cannot parse — so unparseable junk degrades to today's behavior rather than corrupting the date.

- [ ] **Step 5: Run tests**

Run: `./venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 12 passed.

- [ ] **Step 6: Verify against a real browser-scraped company**

Run: `./venv/Scripts/python.exe main.py --test-company "Ryan" --save-raw`
Then check how many rows now carry a date:

```bash
./venv/Scripts/python.exe -c "import pandas as pd; d=pd.read_csv('output/test_company_jobs_raw.csv'); print(d['date_posted'].notna().sum(), 'of', len(d), 'have dates')"
```

Expected: a non-zero count where run3 had zero. If it is still zero, that company's DOM simply has no date near the anchor — try `--test-company "7-Eleven"` before concluding the change failed.

- [ ] **Step 7: Commit**

```bash
git add browser/playwright_scraper.py tests/test_playwright_extract.py
git commit -m "Extract posting dates from the DOM in the Playwright path"
```

---

### Task 4: Extract posting dates in the shared HTML fallback

**Files:**
- Modify: `ats/html_utils.py` (`extract_job_links` near line 55, add `_nearby_date`)
- Modify: `ats/successfactors.py:71-78`
- Modify: `ats/icims.py:68-85`
- Test: `tests/test_html_utils.py`

**Interfaces:**
- Consumes: pytest from Task 1.
- Produces: `extract_job_links(html, base_url, selector=None)` returns dicts with a fourth key `date_posted` (str or `None`), alongside the existing `title`, `job_url`, `location`. Callers that ignore the new key keep working unchanged.

**Why:** All 972 SuccessFactors jobs and 82 iCIMS jobs in run3 have no date, because both collectors' non-JSON-LD fallback path builds records from `extract_job_links`, which never looks for one. `ats/avature.py` uses the same helper.

- [ ] **Step 1: Write the failing test**

Create `tests/test_html_utils.py`:

```python
from ats.html_utils import extract_job_links

HTML = """
<html><body>
  <ul>
    <li>
      <a href="/job/123">Data Engineer</a>
      <span class="jobLocation">Irving, TX</span>
      <span class="jobDate">Aug 18, 2026</span>
    </li>
    <li>
      <a href="/job/456">ETL Developer</a>
      <span class="jobLocation">Frisco, TX</span>
      <time datetime="2026-08-19">yesterday</time>
    </li>
  </ul>
</body></html>
"""

BASE = "https://tenant.example.com/search/"


def test_extracts_date_from_class_marker():
    rows = extract_job_links(HTML, BASE)
    row = next(r for r in rows if r["title"] == "Data Engineer")
    assert row["date_posted"] == "Aug 18, 2026"


def test_prefers_time_datetime_attribute():
    rows = extract_job_links(HTML, BASE)
    row = next(r for r in rows if r["title"] == "ETL Developer")
    assert row["date_posted"] == "2026-08-19"


def test_location_still_extracted():
    rows = extract_job_links(HTML, BASE)
    row = next(r for r in rows if r["title"] == "Data Engineer")
    assert row["location"] == "Irving, TX"


def test_date_is_none_when_absent():
    rows = extract_job_links(
        '<a href="/job/9">Analytics Engineer</a>', BASE
    )
    assert rows[0]["date_posted"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_html_utils.py -v`
Expected: FAIL — `KeyError: 'date_posted'`.

- [ ] **Step 3: Add `_nearby_date` to `ats/html_utils.py`**

Add this function directly below the existing `_nearby_location`:

```python
def _nearby_date(anchor: Any) -> str | None:
    """Look for a posting date in the anchor's immediate neighbourhood.

    Mirrors :func:`_nearby_location`. A ``<time datetime=...>`` attribute is
    preferred because it is machine-formatted; a class-marked element's text
    is the fallback. Returns the raw string - parsing is normalize's job.
    """
    date_pattern = re.compile(r"(date|posted|jobDate|job-date)", re.I)

    container = anchor.parent
    for _ in range(3):
        if container is None:
            break
        time_node = container.find("time")
        if time_node is not None:
            stamp = time_node.get("datetime")
            if stamp:
                return clean_text(stamp)
        node = container.find(attrs={"class": date_pattern})
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if text and len(text) < 60:
                return text
        container = container.parent
    return None
```

- [ ] **Step 4: Return the date from `extract_job_links`**

In `extract_job_links`, change the appended dict to:

```python
        results.append({
            "title": title,
            "job_url": absolute,
            "location": _nearby_location(anchor),
            "date_posted": _nearby_date(anchor),
        })
```

Update the docstring's Returns section to name the fourth key.

- [ ] **Step 5: Pass the date through in SuccessFactors**

In `ats/successfactors.py`, the fallback branch (currently lines 71-78) becomes:

```python
                page_records = [
                    self.record(
                        title=link["title"],
                        location=link.get("location"),
                        date_posted=link.get("date_posted"),
                        job_url=link["job_url"],
                    )
                    for link in extract_job_links(html_text, search_url, selector='a[href*="/job/"]')
                ]
```

- [ ] **Step 6: Pass the date through in iCIMS**

In `ats/icims.py`, both fallback loops (lines 68-85) gain the same argument. The first:

```python
            if not page_records:
                for link in extract_job_links(html_text, search_url, selector="a.iCIMS_Anchor"):
                    page_records.append(
                        self.record(
                            title=link["title"],
                            location=link.get("location"),
                            date_posted=link.get("date_posted"),
                            job_url=link["job_url"],
                        )
                    )
```

And the second, identically but with no `selector` argument:

```python
            if not page_records:
                for link in extract_job_links(html_text, search_url):
                    page_records.append(
                        self.record(
                            title=link["title"],
                            location=link.get("location"),
                            date_posted=link.get("date_posted"),
                            job_url=link["job_url"],
                        )
                    )
```

- [ ] **Step 7: Run tests**

Run: `./venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 16 passed.

- [ ] **Step 8: Verify against a real SuccessFactors company**

Run: `./venv/Scripts/python.exe main.py --test-company "Commercial Metals" --save-raw`
Then:

```bash
./venv/Scripts/python.exe -c "import pandas as pd; d=pd.read_csv('output/test_company_jobs_raw.csv'); print(d['date_posted'].notna().sum(), 'of', len(d), 'have dates')"
```

Expected: a non-zero count (run3 had 0 of 972 across SuccessFactors tenants).

- [ ] **Step 9: Commit**

```bash
git add ats/html_utils.py ats/successfactors.py ats/icims.py tests/test_html_utils.py
git commit -m "Extract posting dates in the shared HTML fallback path"
```

---

### Task 5: Deep multi-layer careers-site traversal

**Files:**
- Modify: `browser/playwright_scraper.py` (`_navigate_to_job_list`, lines 532-591)
- Modify: `config/settings.yaml` (`playwright:` block)
- Test: `tests/test_hop_traversal.py`

**Interfaces:**
- Consumes: `_find_jobs_page_links(page) -> list[dict]` (existing, returns up to 5 `{href, text, score}` sorted by score); `_extract_job_rows`, `_extract_jsonld_rows`, `_dismiss_cookie_banner`, `_click_load_more`, `_search_fallback` (all existing); `detect_ats(url) -> dict` from `ats.detector`.
- Produces: `_navigate_to_job_list(company, page, timeout_ms, max_hops=None) -> PlaywrightResult` with unchanged signature and return type. New module-level helper `_hop_key(url: str) -> str` for visit-deduplication.

**Why this is the main coverage lever:** 33 of run3's 45 failures are `unknown`-provider companies whose careers landing page has no jobs on it. Probing confirmed the jobs sit one to three links away — Centene's landing page links to `/us/en/jobs`, CBRE's to `/en_US/careers/SearchJobs`, Caterpillar's to `/en/jobs/`. The current code hops at most twice and, because of the `break` at line 589, tries only the single highest-scored candidate at each level: one bad guess (e.g. "Life at Centene", which scores on the `/careers/` href hint) burns the entire hop budget.

The replacement is a best-first traversal with three independent limits — depth, total page visits, and a wall-clock budget — so it explores several branches without risking the 240s per-company timeout.

- [ ] **Step 1: Write the failing test**

Create `tests/test_hop_traversal.py`. This tests traversal logic against a local multi-page site served from `file://`-style `set_content` routing, so it needs no network:

```python
import pytest

from browser.playwright_scraper import _hop_key, _navigate_to_job_list

LANDING = """
<html><body>
  <a href="/life">Life at Example</a>
  <a href="/benefits">Benefits</a>
  <a href="/careers/jobs">Jobs</a>
</body></html>
"""

MIDDLE = """
<html><body>
  <a href="/careers/search-jobs">Search Jobs</a>
</body></html>
"""

JOBS = """
<html><body>
  <div><a href="/jobs/1">Senior Data Engineer</a>
       <span class="location">Dallas, TX</span></div>
  <div><a href="/jobs/2">Data Platform Engineer</a>
       <span class="location">Plano, TX</span></div>
</body></html>
"""

PAGES = {
    "https://example.test/": LANDING,
    "https://example.test/life": "<html><body>culture</body></html>",
    "https://example.test/benefits": "<html><body>benefits</body></html>",
    "https://example.test/careers/jobs": MIDDLE,
    "https://example.test/careers/search-jobs": JOBS,
}


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()

    def handler(route):
        url = route.request.url.split("?")[0]
        body = PAGES.get(url) or PAGES.get(url.rstrip("/") + "/")
        if body is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=body)

    ctx.route("**/*", handler)
    p = ctx.new_page()
    p.goto("https://example.test/")
    yield p
    browser.close()
    pw.stop()


def test_hop_key_ignores_trailing_slash_and_case():
    assert _hop_key("https://E.com/Jobs/") == _hop_key("https://e.com/Jobs")


def test_finds_jobs_two_layers_deep(page):
    result = _navigate_to_job_list("Example", page, timeout_ms=5000)
    titles = {j["title"] for j in result.jobs}
    assert "Senior Data Engineer" in titles
    assert "Data Platform Engineer" in titles
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_hop_traversal.py -v`
Expected: FAIL — `ImportError: cannot import name '_hop_key'`.

- [ ] **Step 3: Add traversal settings**

In `config/settings.yaml`, inside the `playwright:` block (after `wait_after_load_ms`), add:

```yaml
  # Careers landing pages often bury the real job list several links deep
  # ("Career Areas" -> "Jobs" -> "Search Jobs"). Traversal is best-first by
  # link score and bounded three ways so one slow site cannot consume the
  # 240s per-company limit: max_hops caps depth, max_hop_visits caps total
  # pages rendered, and hop_budget_seconds caps wall-clock.
  max_hops: 5
  max_hop_visits: 12
  hop_budget_seconds: 100
  # Try the page's own search box at each level, not just at the end. Many
  # job lists render only after a keyword is submitted.
  search_at_each_hop: true
```

- [ ] **Step 4: Add the `_hop_key` helper**

In `browser/playwright_scraper.py`, add near the other module-level helpers (just above `_find_jobs_page_links`):

```python
def _hop_key(url: str) -> str:
    """Normalized identity for visit-deduplication during traversal.

    Case and a trailing slash must not make the same page look new, or the
    traversal can loop between "/jobs" and "/jobs/" until the budget runs out.
    """
    return url.split("#")[0].rstrip("/").lower()
```

- [ ] **Step 5: Replace `_navigate_to_job_list`**

Replace the whole function (lines 532-591) with:

```python
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
    :func:`_find_jobs_page_links`, and bounded by depth, total visits and a
    wall-clock budget so a sprawling site cannot consume the per-company
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

    deadline = time.monotonic() + budget_s
    visited: set[str] = {_hop_key(page.url)}
    visits = 0

    # Frontier entries are (depth, url, score); higher score is explored first.
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
        for _, target, _score in frontier:
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

        _click_load_more(page, max_pages, timeout_ms)
        rows = _extract_job_rows(page) or _extract_jsonld_rows(page)
        if rows:
            log.info("%s: found %s jobs at depth %s -> %s",
                     company, len(rows), depth, target[:90])
            return PlaywrightResult(jobs=rows)

        # This page may be search-driven: the list renders only after a
        # keyword is submitted. Cheap relative to another navigation.
        if search_each and time.monotonic() < deadline:
            searched = _search_fallback(company, page, timeout_ms)
            if searched.jobs or searched.discovered_provider:
                log.info("%s: found jobs via search at depth %s -> %s",
                         company, depth, target[:90])
                return searched

        if depth < max_hops:
            _enqueue(depth + 1)

    return PlaywrightResult()
```

- [ ] **Step 6: Add the `time` import**

Check the top of `browser/playwright_scraper.py`. If `import time` is not already present, add it to the stdlib import block (alongside `random`, `re`, `threading`).

Run: `grep -n "^import time" browser/playwright_scraper.py`

- [ ] **Step 7: Run tests**

Run: `./venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 18 passed.

- [ ] **Step 8: Verify against the three probed companies**

These are known to have their job list buried one or two hops deep:

```bash
./venv/Scripts/python.exe main.py --test-company "Centene"
./venv/Scripts/python.exe main.py --test-company "CBRE"
./venv/Scripts/python.exe main.py --test-company "Caterpillar"
```

Expected: each reports a non-zero "Jobs found" in its diagnostics block, where run3 reported `NoJobsFound`. If a company still fails, run `./venv/Scripts/python.exe tools/probe_site.py "<its url>"` to see what the page actually contains before changing anything — do not guess.

- [ ] **Step 9: Confirm no regression on a company that already worked**

Run: `./venv/Scripts/python.exe main.py --test-company "Ryan"`
Expected: still finds jobs (run3: 16). The deeper traversal must not break the shallow case.

- [ ] **Step 10: Commit**

```bash
git add browser/playwright_scraper.py config/settings.yaml tests/test_hop_traversal.py
git commit -m "Traverse careers sites several layers deep to find job lists"
```

---

### Task 6: Stop Taleo from masking the real failure reason

**Files:**
- Modify: `ats/taleo.py:216-223`
- Test: `tests/test_taleo.py`

**Interfaces:**
- Consumes: `CollectorUnavailable` from `ats.base`.
- Produces: no signature change. `TaleoCollector.collect()` still raises `CollectorUnavailable`, but when both paths fail the message names both.

**Why:** When `_collect_legacy_taleo()` fails and the `_collect_oracle_cloud()` fallback also fails, only the Oracle Cloud exception survives. Run3's log shows every failing Taleo tenant reporting `Oracle Cloud API unavailable: HTTP 404` — but calling the legacy endpoint directly reveals the real cause is `careerSectionUnAvailable: true`. The misleading message cost real debugging time this session.

- [ ] **Step 1: Write the failing test**

Create `tests/test_taleo.py`:

```python
import pytest

from ats.base import CollectorUnavailable
from ats.taleo import TaleoCollector


def test_both_failures_are_reported(monkeypatch):
    collector = TaleoCollector("Example", {"url": "https://ex.taleo.net/careersection/2/jobsearch.ftl"})

    def fail_legacy():
        raise CollectorUnavailable("Taleo searchjobs returned zero requisitions")

    def fail_orc():
        raise CollectorUnavailable("Oracle Cloud API unavailable: HTTP 404")

    monkeypatch.setattr(collector, "_collect_legacy_taleo", fail_legacy)
    monkeypatch.setattr(collector, "_collect_oracle_cloud", fail_orc)

    with pytest.raises(CollectorUnavailable) as excinfo:
        collector.collect()

    message = str(excinfo.value)
    assert "zero requisitions" in message
    assert "HTTP 404" in message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_taleo.py -v`
Expected: FAIL — the message contains only "HTTP 404".

- [ ] **Step 3: Report both failures**

Replace `TaleoCollector.collect()` (lines 216-223) with:

```python
    def collect(self) -> list[dict]:
        if self._is_oracle_cloud():
            return self._collect_oracle_cloud()
        try:
            return self._collect_legacy_taleo()
        except CollectorUnavailable as legacy_exc:
            # Some tenants sit on oraclecloud behind a taleo.net vanity host.
            try:
                return self._collect_oracle_cloud()
            except CollectorUnavailable as orc_exc:
                # Report both: the legacy failure is usually the real reason
                # (e.g. careerSectionUnAvailable), and reporting only the ORC
                # 404 sends debugging down the wrong path.
                raise CollectorUnavailable(
                    f"legacy Taleo: {legacy_exc}; Oracle Cloud fallback: {orc_exc}"
                ) from legacy_exc
```

- [ ] **Step 4: Run tests**

Run: `./venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 19 passed.

- [ ] **Step 5: Verify against a real failing tenant**

Run: `./venv/Scripts/python.exe main.py --test-company "Texas Health Resources"`
Expected: the warning line now contains both `legacy Taleo:` and `Oracle Cloud fallback:`.

- [ ] **Step 6: Commit**

```bash
git add ats/taleo.py tests/test_taleo.py
git commit -m "Report both Taleo failure reasons instead of masking the first"
```

---

### Task 7: Recover Workday tenants behind vanity hostnames

**Files:**
- Modify: `ats/detector.py` (Workday branch of the coordinate extractor)
- Test: `tests/test_detector_workday.py`

**Interfaces:**
- Consumes: `detect_ats(url) -> dict` and `detect_from_html(html, final_url=...) -> dict` (existing).
- Produces: no signature change.

**Why:** Four companies (USAA, Frost Bank, Blue Cross/HCSC, Abbott Laboratories) fail with `Incomplete Workday coordinates (host=..., tenant=None, site=None)`. Their careers URLs are vanity domains (`www.usaajobs.com`, `careers.frostbank.com`, `www.jobs.abbott`) rather than the standard `{tenant}.wdN.myworkdayjobs.com`, so tenant/site cannot be parsed from the URL.

**Investigate before editing.** The fix depends on what the vanity page actually exposes — do not assume. Run:

```bash
./venv/Scripts/python.exe tools/probe_site.py "https://www.usaajobs.com/" "https://careers.frostbank.com/" "https://www.jobs.abbott/us/en"
```

If the probe shows these pages redirect to, or embed, a real `*.myworkdayjobs.com` URL, the correct fix is in `ats/resolver.py`'s page-resolution path (make the embedded Workday URL win over the vanity host). If instead the vanity host serves the Workday CXS API directly under a discoverable tenant/site, the fix is in `ats/detector.py`'s coordinate extraction. Choose based on the probe output.

- [ ] **Step 1: Write the failing test**

Create `tests/test_detector_workday.py`. Fill the `VANITY_HTML` constant with a real snippet from the probe output above (the line containing the `myworkdayjobs.com` reference), so the test asserts against a real page shape rather than an invented one:

```python
from ats.detector import detect_from_html


def test_vanity_host_resolves_workday_coordinates():
    # Snippet captured from the live careers page via tools/probe_site.py.
    html = '<a href="https://usaa.wd1.myworkdayjobs.com/en-US/USAAJOBS">Search</a>'
    result = detect_from_html(html, final_url="https://www.usaajobs.com/")
    assert result["provider"] == "workday"
    assert result["tenant"] == "usaa"
    assert result["site"] == "USAAJOBS"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/test_detector_workday.py -v`
Expected: FAIL, with `tenant` being `None` or the assertion on `provider` failing.

- [ ] **Step 3: Implement the fix indicated by the probe**

Make the smallest change that satisfies the test: ensure that when an embedded/redirect `*.myworkdayjobs.com` URL is found, its tenant and site are extracted from *that* URL rather than from the vanity host. The existing Workday URL parser already handles the standard shape — reuse it on the discovered URL instead of duplicating the regex.

- [ ] **Step 4: Run tests**

Run: `./venv/Scripts/python.exe -m pytest tests/ -v`
Expected: 20 passed.

- [ ] **Step 5: Verify all four companies**

```bash
./venv/Scripts/python.exe main.py --test-company "USAA"
./venv/Scripts/python.exe main.py --test-company "Frost Bank"
./venv/Scripts/python.exe main.py --test-company "Abbott"
./venv/Scripts/python.exe main.py --test-company "Blue Cross"
```

Expected: each reports non-zero jobs. Any that still fail should be recorded honestly in the final report rather than forced — some vanity hosts genuinely do not expose their tenant.

- [ ] **Step 6: Commit**

```bash
git add ats/detector.py tests/test_detector_workday.py
git commit -m "Resolve Workday coordinates for vanity careers hostnames"
```

---

### Task 8: Canary smoke test

**Files:**
- Create: `tools/canary.py`

**Interfaces:**
- Consumes: `pipeline.run` / the same entry point `main.py` uses; `ats.router.plan_route`.
- Produces: a CLI — `./venv/Scripts/python.exe tools/canary.py` — exiting 0 when every canary company returns jobs, 1 otherwise.

**Why:** Two full runs were wasted this session before it was noticed that every browser-routed company was failing. A full run takes ~14 minutes; this check takes under a minute and would have caught it immediately.

- [ ] **Step 1: Write the canary**

Create `tools/canary.py`:

```python
"""Pre-flight check: is each major collection path still working?

Not part of the pipeline - run it before trusting a full run. A full run
takes ~14 minutes; this takes under a minute and catches the class of
breakage where one provider (or the whole browser path) silently returns
zero jobs for every company.

    python tools/canary.py
    python tools/canary.py --quiet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats.router import fetch_company_jobs  # noqa: E402
from logger import setup_logging  # noqa: E402
from settings import load_settings  # noqa: E402

# One company per collection path, chosen because each returned jobs
# reliably in the 2026-08-21 baseline run.
CANARIES = [
    ("Capital One", "workday"),
    ("TPG", "greenhouse"),
    ("Match Group", "lever"),
    ("Texas Instruments", "taleo"),
    ("RealPage", "icims"),
    ("BNSF Railway", "phenom"),
    ("Commercial Metals Company (CMC)", "successfactors"),
    ("GameStop", "ukg"),
    ("Ryan", "playwright"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    setup_logging("logs/canary.log", "WARNING", quiet=True)
    cfg = load_settings()

    import pandas as pd
    excel_path = cfg.resolve_path("input_excel", "config/companies.xlsx")
    companies = pd.read_excel(excel_path)
    columns = cfg.get("columns", {})
    name_col = columns.get("company", "Company")
    ats_col = columns.get("ats_url", "ATS URL")
    live_col = columns.get("live_jobs_url", "Live Jobs Page (if ATS URL unavailable)")

    failures = []
    for company, expected_path in CANARIES:
        row = companies[companies[name_col].astype(str).str.strip() == company]
        if row.empty:
            print(f"  SKIP  {company:<38} not in workbook")
            continue
        record = row.iloc[0]
        result = fetch_company_jobs(
            company,
            ats_url=record.get(ats_col),
            live_jobs_url=record.get(live_col),
        )
        count = len(result.jobs)
        status = "OK  " if count > 0 else "FAIL"
        if count == 0:
            failures.append((company, expected_path, result.error_message))
        if not args.quiet or count == 0:
            print(f"  {status}  {company:<38} {expected_path:<15} {count:>5} jobs")

    print()
    if failures:
        print(f"CANARY FAILED: {len(failures)} of {len(CANARIES)} paths returned zero jobs")
        for company, path, error in failures:
            print(f"  {company} ({path}): {error}")
        return 1
    print(f"CANARY PASSED: all {len(CANARIES)} collection paths returned jobs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it**

Run: `./venv/Scripts/python.exe tools/canary.py`
Expected: `CANARY PASSED: all 9 collection paths returned jobs`, in well under a minute.

If any canary fails, that is a real finding — fix the underlying path before continuing, or replace that canary company if the company itself (not the path) has changed.

- [ ] **Step 3: Verify it actually detects breakage**

Temporarily break the browser path to confirm the canary catches it: in `browser/playwright_scraper.py`, at the top of `_extract_job_rows`, add `return []`. Run the canary — the "Ryan / playwright" line must report FAIL and the exit code must be 1 (`echo $?`). Then remove the injected line and re-run to confirm it passes again.

- [ ] **Step 4: Commit**

```bash
git add tools/canary.py
git commit -m "Add canary smoke test for every collection path"
```

---

### Task 9: Full verification run and honest reporting

**Files:**
- Modify: `config/settings.yaml` (concurrency, only if the run supports it)
- Modify: `README.md`

**Interfaces:**
- Consumes: everything from Tasks 1-8.
- Produces: the final coverage number and an updated README.

- [ ] **Step 1: Confirm no pipeline process is already running**

Run: `ps aux | grep -i python`
Expected: no `main.py` process. Two concurrent runs corrupt `data/jobs.db` and `config/companies.xlsx`.

- [ ] **Step 2: Run the canary first**

Run: `./venv/Scripts/python.exe tools/canary.py`
Expected: PASSED. Do not start a full run on a failing canary.

- [ ] **Step 3: Start the full run in the background**

Run in the background (no `nohup`, no trailing `&` — the harness backgrounds it; combining the two corrupts Playwright's sync API):

```bash
./venv/Scripts/python.exe main.py --save-raw > run_output4.log 2>&1
```

- [ ] **Step 4: Compare against the baseline**

When it completes, read the summary block from `run_output4.log` and compare every metric to run3:

| Metric | run3 baseline |
|---|---|
| Companies successful | 123 / 171 |
| Jobs collected | 39,947 |
| Target data jobs | 216 |
| DFW/Remote matches | 36 |
| Within window | 18 |
| Wall-clock | 13.6 min |

Also check the date-coverage improvement:

```bash
./venv/Scripts/python.exe -c "import pandas as pd; d=pd.read_csv('output/company_jobs_raw.csv'); print(d['date_posted'].notna().sum(), 'of', len(d), 'have dates')"
```

Baseline was 34,312 of 39,947 (5,635 missing).

- [ ] **Step 5: Check for regressions company-by-company**

Any company that succeeded in run3 but fails now is a regression and must be investigated before the work is called done:

```bash
./venv/Scripts/python.exe -c "
import pandas as pd
old = set(pd.read_csv('output/scraper_failures.csv').company)
print('Compare this list against run_output3.log failures manually.')
print(sorted(old))
"
```

- [ ] **Step 6: Consider the concurrency bump**

Only if the run completed cleanly with no new `Timeout` failures: raise `concurrency.playwright_workers` from 3 to 5 in `config/settings.yaml`, re-run, and keep the change only if company success count does not drop. Each worker holds a full headless Chromium instance; contention shows up as new `Timeout` errors. If success count drops, revert to 3 and note it.

- [ ] **Step 7: Update the README**

Update these sections to match reality:
- The pipeline diagram's `72-hour filter` box (the window is configurable; the label should not name a fixed number).
- The Output section's `date_filter_status` values (now `within_window` / `older_than_window` / `date_unavailable`).
- The Configuration section: document `playwright.max_hops`, `max_hop_visits`, `hop_budget_seconds`, `search_at_each_hop`.
- The Project layout: add `tools/canary.py` and `tests/`.

- [ ] **Step 8: Commit**

```bash
git add config/settings.yaml README.md
git commit -m "Document traversal settings and refresh README after verification run"
```

---

## Deviations from the spec

Two spec items were dropped after evidence gathered while writing this plan
contradicted the assumption behind them. Both deletions are deliberate.

1. **`dateparser` is not needed.** The spec assumed dates were arriving in
   formats the existing parser could not read. Checking run3's 39,947 raw
   rows shows the opposite: every date that *is* present parses correctly,
   and all 5,635 missing ones come from three code paths that never populate
   `date_posted` at all (`_extract_job_rows` hardcodes `None`;
   `extract_job_links` never looks for a date). It is an extraction gap, not
   a parsing gap — adding a parser would have fixed nothing. Tasks 3 and 4
   fix the actual cause.

2. **`curl_cffi` is not needed yet.** The spec scoped it to hosts still
   blocked after the User-Agent fix. That set turned out to be empty:
   RealPage now collects 81 jobs through the direct iCIMS API and State
   Farm's API returns cleanly. Adding a TLS-impersonation dependency with no
   remaining host to point it at would be speculative. Revisit only if a
   future run shows a WAF block that the UA change does not clear.

A third spec item is **already satisfied** and needs no task: the proposed
`record_incomplete` flag duplicates existing behavior — `build_record`
already returns `None` when `title` or `job_url` is missing, so malformed
records never reach the CSV. Task 1 adds a test locking that in.

## Notes on the 90% target

The goal is 154/171. Tasks 5 and 7 are the coverage levers; Tasks 3, 4, and 2 are accuracy. Realistically:

- 33 `unknown`-provider failures are the deep-traversal population (Task 5). Probing three of them found a reachable job list in all three, but that will not generalise to all 33.
- 4 Workday vanity hosts (Task 7).
- Several are genuinely out of reach without the tools explicitly excluded from this round: Salesforce (job list loads via client-side GraphQL after render, nothing in server HTML), Nokia and Samsung (bot-blocked / non-English portal behind login), CGI (returns zero bytes).

If the final number lands short of 154, report the actual figure and the specific reason each remaining company fails. Do not describe a partial result as if it hit the target.
