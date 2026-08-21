# Company ATS Job Scraper

Scrapes jobs **directly from company ATS systems and career pages**, driven by an
Excel workbook of companies. Built for a Dallas–Fort Worth data-engineering job
search, but the roles, locations and freshness window are all configurable.

> **Independent of JobSpy.** This pipeline has its own input, virtualenv,
> SQLite database and output directory. It never reads, merges with, or
> deduplicates against JobSpy results.

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

Only *verified* discoveries are written back — ones whose collector actually
returned jobs. A URL that merely pattern-matches an ATS never lands in the
workbook. Existing cell values are never overwritten, and the workbook is
backed up before every write.

Real example: GameStop's careers page links out to `gamestop.rec.pro.ukg.net`.
Browser scraping of the landing page returned 0 jobs; the discovered UKG API
returned 2,500.

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
| Eightfold | `/api/apply/v2/jobs` | Uses `?domain=` when present |
| UKG Pro | `JobBoardView/LoadSearchResults` | `ultipro.com` and `*.ukg.net` hosts |
| Phenom | `phApp.ddo` on `/search-results` | Page-embedded JSON, not the dead `/widgets` endpoint |
| Oracle Cloud | `recruitingCEJobRequisitions` | Requires `expand=requisitionList` |
| Taleo (legacy) | HTML via browser | REST API needs session/CSRF state; browser path used instead |
| iCIMS | HTML + JSON-LD | No public API |
| SuccessFactors | HTML + JSON-LD | OData requires tenant auth |
| Avature | HTML + JSON-LD | No public API |

Any collector that cannot serve a tenant raises `CollectorUnavailable`, and the
router falls back to Playwright rather than failing the company.

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

---

## Reliability

- 30s HTTP timeout, 3 retries, exponential backoff (tenacity), `Retry-After` honoured on 429
- Navigation retries with rotated user-agent/viewport
- `playwright-stealth` patches the fingerprints headless Chromium leaks, which
  some career sites gate on
- Per-company exception isolation — one failure never stops the run
- Each worker thread closes its own Playwright instance: the sync API is
  thread-affine and cross-thread teardown deadlocks

---

## Project layout

```
company_job_scraper/
├── config/          settings.yaml, companies.xlsx
├── ats/             detector, resolver, url_repair, router + 13 collectors
├── browser/         playwright_scraper.py (search, hop, stealth, discovery)
├── tools/           probe_site.py (diagnostic, not part of the pipeline)
├── normalize.py filters.py deduplicate.py enrich.py job_identity.py
├── database.py logger.py http_client.py settings.py export_ats_urls.py
├── pipeline.py main.py
└── requirements.txt
```
