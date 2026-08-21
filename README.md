# Company ATS Job Scraper

Scrapes jobs **directly from company ATS systems and career pages**, driven by an
Excel workbook of companies. Built for a Dallas–Fort Worth data-engineering job
search, but the roles, locations and freshness window are all configurable.

> **Independent of JobSpy.** This pipeline has its own input, virtualenv,
> SQLite database and output directory. It never reads, merges with, or
> deduplicates against JobSpy results.

> **New here?** Jump to the [Codebase map](#codebase-map) to find the right
> file fast, or [How it works](#how-it-works) for the routing model. Design
> rationale lives in [`docs/superpowers/`](docs/superpowers/README.md).

---

## How it works

```
companies.xlsx
      │
      ▼
  URL repair ──── dead careers.* subdomain? find the live careers page
      │
      ▼
   router ─── ATS URL present? ──▶ detect provider ──▶ Direct ATS API ──┐
      │                                                                 │
      │  no ATS URL                                                     │
      ▼                                                                 │
  fetch career page ──▶ ATS embedded/redirected? ──▶ Direct ATS API ────┤
      │                                                                 │
      │  still unknown                                                  │
      ▼                                                                 │
  JSON-LD tier ──▶ page embeds schema.org JobPosting? ──▶ harvest ──────┤
      │                                                                 │
      │  still nothing                                                  │
      ▼                                                                 │
  Playwright ──▶ extract job links                                      │
      ├──▶ nothing? type "Data Engineer" into the page's search box     │
      ├──▶ nothing? hop to "Search jobs" / "View openings" page         │
      └──▶ found an ATS link? ──▶ switch to its API mid-run ────────────┤
                                                                        │
                                        ┌───────────────────────────────┘
                                        ▼
                                    normalize
                                        ▼
                                target-role filter
                                        ▼
                            enrich coarse locations
                                        ▼
                              DFW / remote filter
                                        ▼
                              freshness filter
                                        ▼
                          internal deduplication
                                        ▼
                    SQLite tracking (new / removed jobs)
                                        ▼
                          output/company_jobs.csv
```

### Self-healing ATS discovery

When the browser finds a real ATS behind a branded careers page, the router
switches to that provider's API **in the same run**, then writes the URL back
into the workbook's blank `ATS URL` cell so later runs skip discovery entirely.
The same write-back also fires when the *cheaper* page-resolution step
(`ats/resolver.py`, one HTTP GET, no browser) finds the ATS instead — not just
the Playwright path.

Only *verified* discoveries are written back — ones whose collector actually
returned jobs. A URL that merely pattern-matches an ATS never lands in the
workbook. Existing cell values are never overwritten, and the workbook is
backed up before every write.

Real example: GameStop's careers page links out to `gamestop.rec.pro.ukg.net`.
Browser scraping of the landing page returned 0 jobs; the discovered UKG API
returned 2,500.

A dead `careers.*` subdomain that `ats/url_repair.py` swaps for a live one
(see below) gets the one deliberate exception to "never overwritten": once
the repaired URL is verified by actually returning jobs this run, it replaces
the *exact* dead value it came from — never a value that isn't the one repair
just fixed — so later runs start from the live page instead of re-repairing
the same dead one every time.

---

## Entry points & tools

There is **one shared engine** (`ats/detector`, `ats/resolver`, `ats/discovery`
and the collectors) behind everything below — these are different front doors to
it, not separate codebases.

| Command | What it does | Scrapes jobs? | Writes to workbook |
|---------|--------------|:---:|--------------------|
| `python main.py` | **The full pipeline.** Reads Excel → for each company gets the ATS/job URL **and collects the jobs** → normalize/filter/dedupe → `output/company_jobs.csv`. Discovers ATS URLs itself along the way (self-heal). | ✅ | verified URLs → the real `ATS URL` cell |
| `python tools/find_ats_urls.py` | **Discovery only.** Crawls to find & *verify* an ATS URL / job-search page per company, then stops — no job list, no filtering. | ❌ | suggestions → `Suggested…` columns (or the real columns with `--apply`) |
| `python tools/canary.py` | **Smoke test.** One company per collection path (~2 min); exits non-zero if any path returns zero jobs. Run before trusting a full run. | tests only | no |
| `python tools/probe_site.py <url>` | **Diagnostic.** Dumps what a single page actually contains (links, detected provider). For investigating one stubborn site. | no | no |

**Do I need the discovery tool?** No — `main.py` already discovers and back-fills
ATS URLs on its own. `find_ats_urls.py` is an optional *pre-pass*: bulk-fill or
repair the URL column up front (especially companies stuck on the slow Playwright
path), review suggestions before trusting them, and get an explicit `NOT FOUND`
list to fix by hand — all without a full ~15-minute scrape. Typical loop: run
`find_ats_urls.py --only-failures` occasionally to enrich the workbook, eyeball
and `--apply` the good ones, then every `main.py` run is faster and hits more
direct APIs. Never run it during a full run — both write `config/companies.xlsx`.

---

## Setup

Requires **Python 3.10+**.

```bash
python -m venv venv
```

```bash
venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
playwright install chromium
```

Copy `config/companies.example.xlsx` to `config/companies.xlsx` and fill in your
companies:

| Company | ATS URL | Live Jobs Page (if ATS URL unavailable) |
|---------|---------|------------------------------------------|

Leave `ATS URL` blank when you don't know it — the pipeline will try to
discover and fill it for you.

---

## Usage

```bash
python main.py
```

| Flag | Effect |
|------|--------|
| `--dry-run` | Read Excel, detect providers, print routing, scrape nothing |
| `--resolve` | With `--dry-run`, also probe branded pages (1 GET each) |
| `--test-company NAME` | Only companies matching NAME, with diagnostics |
| `--test-provider P` | Only companies routed to provider P |
| `--limit N` | Process only the first N companies |
| `--no-playwright` | Disable browser fallback |
| `--no-resolve` | Skip page resolution and URL repair |
| `--no-write-back` | Don't write discovered ATS URLs into the workbook |
| `--save-raw` | Also write every collected job, pre-filter |
| `--quiet` | Log to file only |
| `--config PATH` / `--excel PATH` | Override config or input workbook |

Partial runs (`--test-company`, `--test-provider`, `--limit`) write to
`test_`-prefixed output files and never modify the workbook, so they cannot
clobber a full run's results.

---

## Supported ATS providers

| Provider | Method | Notes |
|----------|--------|-------|
| Workday | `/wday/cxs/{tenant}/{site}/jobs` | Enriches multi-location reqs via job detail |
| Greenhouse | `boards-api.greenhouse.io` | Documented public API |
| Lever | `api.lever.co/v0/postings` | Documented public API |
| Ashby | `api.ashbyhq.com/posting-api` | Documented public API |
| SmartRecruiters | `api.smartrecruiters.com` | Documented public API |
| Paylocity | `recruiting.paylocity.com` | Tenant + board GUID from the URL |
| Eightfold | `/api/apply/v2/jobs` | Uses `?domain=` when present |
| UKG Pro | `JobBoardView/LoadSearchResults` | `ultipro.com` and `*.ukg.net` hosts |
| Phenom | `phApp.ddo` on `/search-results` | Page-embedded JSON, not the dead `/widgets` endpoint |
| Oracle Cloud | `recruitingCEJobRequisitions` | Requires `expand=requisitionList` |
| Taleo (legacy) | HTML via browser | REST API needs session/CSRF state; browser path used instead |
| iCIMS | HTML + JSON-LD | No public API |
| SuccessFactors | HTML + JSON-LD | OData requires tenant auth |
| Avature | HTML + JSON-LD | No public API; self-hosted portals detected via `avature.portal` fingerprint |
| Radancy (TalentBrew) | `/search-jobs/results` JSON fragment | Runs on the company's own domain; detected by HTML fingerprint, not host |
| Amazon | `amazon.jobs/search.json` | Amazon's own careers API, not a third-party ATS |
| Jobvite | `jobs.jobvite.com/{tenant}` | Server-rendered list; tenant is the first path segment |
| Cornerstone (CSOD) | `career-site/v1/search` | Token-gated; JWT lifted from the careersite home page |
| Jibe (iCIMS) | `{tenant}.jibeapply.com/api/jobs` | Public JSON search API |

Beyond these host/fingerprint-matched providers, a **generic JSON-LD tier**
(`ats/jsonld.py`) harvests any page's `schema.org/JobPosting` structured data
over a single HTTP GET before the browser fallback runs — so an unknown
provider that embeds JobPosting markup is still collected cheaply.

Any collector that cannot serve a tenant raises `CollectorUnavailable`, and the
router falls back to the next tier (JSON-LD, then Playwright) rather than
failing the company.

---

## Output

`output/company_jobs.csv` and `.json`:

```
company, title, location, date_posted, job_url, apply_url, employment_type,
remote, description, ats_provider, scraping_method, date_filter_status,
location_match_type, first_seen, is_new
```

`date_filter_status` is `within_window`, `older_than_window`, or
`date_unavailable`. Jobs with no reliable posting date are **kept and flagged**,
never silently discarded — browser-scraped pages rarely expose a trustworthy
date, and inventing one would corrupt the freshness filter.

`output/scraper_failures.csv` — one row per failed company with `error_type`,
`error_message` and `timestamp`.

---

## Job tracking (SQLite)

`data/jobs.db` tracks every job seen, keyed on a **stable job id** derived from
the posting URL rather than the URL itself — retitling a job changes its URL
slug but not its underlying requisition id, so the same job stays the same row.

After each successful company scrape, jobs no longer listed by that company are
deleted. The comparison is scoped per-company via an index (measured at 0.38ms
against a 21,000-row table), never a full-table scan. Companies whose scrape
*failed* are skipped entirely — a scraping hiccup must never be read as "all
jobs closed".

---

## Configuration

Everything tunable lives in `config/settings.yaml`: the freshness window
(`hours_old`), DFW city list, target-role and exclusion regexes, HTTP
timeout/retry/backoff, Playwright behaviour (including stealth and the search
fallback term), and concurrency (`http_workers: 10`, `playwright_workers: 3`).

Target roles are matched **per title segment** — titles are split on `,`, `/`,
`-`, `|` and each segment tested — so `Software Engineer, Data Engineering`
matches while `Data Scientist`, `Software Engineer` and `Machine Learning
Engineer` do not.

DFW matching rejects same-named cities in other states, so `Westlake Village, CA`
and `Richardson, UT` do not pass as DFW.

### Careers-site traversal

Most workbook rows give a branded careers page, not an ATS URL, and the real
job list is often several links deep (`Career Areas` → `Jobs` → `Search Jobs`).
The browser fallback explores best-first by link score, bounded three ways so
one sprawling site cannot burn the per-company timeout:

| Setting | Default | Effect |
|---------|---------|--------|
| `playwright.max_hops` | 5 | How many links deep to follow |
| `playwright.max_hop_visits` | 12 | Total pages rendered per company |
| `playwright.hop_budget_seconds` | 100 | Wall-clock ceiling for traversal |
| `playwright.search_at_each_hop` | true | Try each page's own search box, not just the last |
| `playwright.hop_good_enough_rows` | 10 | Rows that count as a real list, not "featured" roles |

The last one matters: landing pages routinely show three featured roles.
Returning those would report 3 jobs for a company with thousands, so a small
result is kept only as a fallback while the search continues.

### Two User-Agents, deliberately

`requests.user_agent` is a bare `Mozilla/5.0` because a full Chrome UA trips
AWS WAF's bot captcha on several iCIMS tenants. `playwright.user_agent` is a
full Chrome string because Cloudflare rejects the bare one outright in a real
browser context. Neither value works for both paths — changing either to match
the other silently costs coverage.

---

## Reliability

- 30s HTTP timeout, 3 retries, exponential backoff (tenacity), `Retry-After` honoured on 429
- Navigation retries with rotated user-agent/viewport
- `playwright-stealth` patches the fingerprints headless Chromium leaks, which
  some career sites gate on
- Per-company exception isolation — one failure never stops the run
- Each worker thread closes its own Playwright instance: the sync API is
  thread-affine and cross-thread teardown deadlocks
- A failed Chromium launch tears down the Playwright driver it had already
  started, so the driver's event loop can't linger and poison the next
  company on that worker with "Sync API inside the asyncio loop"
- A clean render that comes back with zero jobs is retried like a navigation
  error, up to the same attempt budget (`playwright.nav_retries`) - confirmed
  against a same-day pair of full runs where Nokia, Ericsson and CBRE each
  found real jobs in one run and came back empty in the other under the same
  3-worker concurrency, but worked every time re-verified in isolation

---

## Codebase map

Start here to find the right file without reading the whole tree. The pipeline
splits cleanly into **routing/collection** (how a company's jobs are reached)
and the **post-scrape tail** (normalize → filter → dedupe → store → output).

### Entry points & orchestration

| File | Responsibility |
|------|----------------|
| `main.py` | CLI: arg parsing, `--dry-run`/`--test-*` modes, wiring to `pipeline.run()` |
| `pipeline.py` | Run orchestration — load workbook → route → execute (2 thread pools) → filter/dedupe/store → write outputs & workbook write-back |
| `settings.py` | Loads `config/settings.yaml`; path resolution and config access |
| `logger.py` | Logging setup |
| `http_client.py` | Shared `requests` session, retries/backoff, `get_json`/`get_text`/`post_json` |

### Routing & detection (`ats/`)

| File | Responsibility |
|------|----------------|
| `ats/router.py` | The ladder: `plan_route()` (decide provider+method) and `fetch_company_jobs()` (API → JSON-LD → Playwright, with mid-run self-heal). `COLLECTORS` dict = supported providers |
| `ats/detector.py` | Lexical ATS detection from a URL, plus HTML fingerprints and embedded-URL extraction. **Add a new provider's host/fingerprint here** |
| `ats/resolver.py` | One HTTP GET on a branded page → identify the ATS behind it (redirect/fingerprint/embedded URL); 403→browser-UA retry |
| `ats/url_repair.py` | Swaps a dead `careers.*` subdomain for a live careers page before routing |
| `ats/base.py` | `ATSCollector` base class + `CollectorUnavailable`; `record()`/`finalize()` helpers every collector uses |
| `ats/html_utils.py` | Shared HTML/JSON-LD parsing helpers for collectors |
| `ats/discovery.py` | On-demand ATS-URL discovery engine (used by `tools/find_ats_urls.py`, **not** the live pipeline) |

### Collectors (`ats/`, one per provider — 18 + generic tier)

| File | Provider |
|------|----------|
| `workday.py` `greenhouse.py` `lever.py` `ashby.py` `smartrecruiters.py` | documented public APIs |
| `paylocity.py` `ukg.py` `taleo.py` `icims.py` `phenom.py` | |
| `successfactors.py` `avature.py` `eightfold.py` `radancy.py` | |
| `amazon.py` `jobvite.py` `cornerstone.py` `jibe.py` | added in the coverage-expansion branch |
| `jsonld.py` | **generic** schema.org JobPosting tier — provider-agnostic fallback |

To add a provider: register its host/fingerprint in `detector.py`, write
`ats/<provider>.py` subclassing `ATSCollector`, add it to `COLLECTORS` in
`router.py`, and add an offline test. Ship it only once a real workbook company
returns real jobs through it.

### Browser fallback (`browser/`)

| File | Responsibility |
|------|----------------|
| `browser/playwright_scraper.py` | Keyword-search + best-first hop traversal, JSON-LD extraction, cookie dismissal, network sniffing for ATS discovery, stealth, retry with rotated fingerprint |

### Post-scrape tail (the part you said won't change)

| File | Responsibility |
|------|----------------|
| `normalize.py` | Build the canonical job record; clean text, parse dates, join locations |
| `filters.py` | Role match (per title segment), DFW/remote match, freshness window |
| `enrich.py` | Fill coarse locations (e.g. Workday detail fetch) |
| `deduplicate.py` | Collapse duplicate postings within a run |
| `job_identity.py` | Stable per-job id derived from the posting URL |
| `database.py` | SQLite tracking — upsert, per-company removal sync, new/first-seen |
| `export_ats_urls.py` | Write verified discovered ATS URLs, verified dead-URL repairs, and run status back into the workbook |

### Config, tools, tests, docs

| Path | Responsibility |
|------|----------------|
| `config/settings.yaml` | All tunables (freshness, roles, DFW cities, HTTP, Playwright, concurrency) |
| `config/companies.xlsx` | Input workbook (Company / ATS URL / Live Jobs Page) |
| `tools/canary.py` | ~2-min smoke test: one company per collection path (run before a full run) |
| `tools/find_ats_urls.py` | Crawl + verify missing ATS URLs, write suggestions into the workbook |
| `tools/probe_site.py` | Diagnostic: dump what a single page actually contains |
| `tests/` | Offline pytest suite (network mocked) |
| `docs/superpowers/` | Design specs + implementation plans — see `docs/superpowers/README.md` for the index |

## Before trusting a full run

A full run takes ~15 minutes. `tools/canary.py` checks one company per
collection path in about two minutes and exits non-zero if any path returns
zero jobs — it catches the case where a whole provider (or the browser path)
breaks silently:

```bash
python tools/canary.py
```

Unit tests: `python -m pytest tests/ -v`

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

---

## Keeping this doc current

**This README is the single source of truth for how the repo works.** It is
meant to be maintained in lockstep with the code — read it to understand the
project, and update it in the *same change* whenever you touch the things it
describes. When you change the code, update the matching section:

| If you change… | Update this section |
|----------------|---------------------|
| A collector, or `COLLECTORS` in `ats/router.py` | [Supported ATS providers](#supported-ats-providers) + collector list in [Codebase map](#codebase-map) |
| Detection / resolution / routing flow | [How it works](#how-it-works) diagram |
| A CLI flag in `main.py` | [Usage](#usage) flag table |
| An entry-point script or tool in `tools/` | [Entry points & tools](#entry-points--tools) |
| Any module's purpose, or add/remove a file | [Codebase map](#codebase-map) |
| A setting in `config/settings.yaml` | [Configuration](#configuration) |
| Output columns or filtering behaviour | [Output](#output) |
| A design decision worth recording | add/refresh a doc under [`docs/superpowers/`](docs/superpowers/README.md) |

If a change makes a section wrong, fixing the doc is part of finishing the
change — not a follow-up.
