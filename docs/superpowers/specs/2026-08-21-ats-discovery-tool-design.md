# Design: automated ATS URL discovery and career-page repair

Date: 2026-08-21
Status: approved, pending spec review

## Context

The workbook drives everything: each company gives an `ATS URL` or a
`Live Jobs Page`. When both are missing or the page is a marketing page
rather than a job list, the company yields nothing.

The user filled this gap by hand — pasting the sheet into Claude chat, which
searched the web for each company's ATS, judged whether a URL was that
company's own tenant, and replaced marketing pages with real job-search
pages. It fixed 29 companies in one pass and raised the sheet from 53 to 72
ATS URLs.

The question this spec answers: **can the pipeline do that itself?**

Partly. Claude chat used web search plus judgment. Code cannot judge, so it
substitutes something stricter — **verification**. It does not decide whether
a page *looks* like the right careers site; it drives every candidate through
a real collector and keeps only what actually returns jobs.

That distinction matters, because the hand-curated pass shows what happens
without verification. Of the 72 ATS URLs now in the sheet, 18 do not resolve
to a supported provider, and several are wrong in ways a person would not
notice at a glance:

- `infosys.com/careers/`, `tcs.com/careers/united-states`,
  `careers.hcltech.com/` — marketing pages sitting in the `ATS URL` column.
- `walmart.wd5.myworkdayjobs.com/WalmartExternal/` — a correctly-shaped
  Workday URL whose CXS API returns HTTP 422 for every request body tried.

Several others are genuinely useful even though they are not ATS platforms —
`higher.gs.com/results`, `ibm.com/careers/search`,
`careers.fiserv.com/us/en/search-results` are real listings pages, so the
browser now lands on results instead of marketing copy.

### Worked example, verified during design

Nokia was previously recorded as unfixable ("blocks automated traffic").
That is wrong. `jobs.nokia.com/en/sites/CX_1/jobs` is an Oracle Cloud
Recruiting site whose HTML embeds its API host. Fetching that page and
extracting the host yields:

```
https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com
  /hcmRestApi/resources/latest/recruitingCEJobRequisitions
  ?finder=findReqs;siteNumber=CX_1,...
```

which returns **575 jobs** (verified 2026-08-21). The existing
`TaleoCollector` already drives this shape: `detect_ats` maps the
`oraclecloud.com` host to `taleo`, `_is_oracle_cloud()` returns True, and
`_site_number()` defaults to `CX_1`, which is correct here.

The only reason the pipeline misses it: `_EMBEDDED_URL_PATTERNS[TALEO]` in
`ats/detector.py` matches `taleo.net` only, never `oraclecloud.com`, so
`extract_any_embedded_ats_url` never surfaces the host.

This one example is the whole design in miniature — crawl the page, extract
the embedded ATS URL, verify it returns jobs, write it back — and it is why
the tool is worth building rather than continuing by hand.

## Goal

An on-demand tool that, for companies whose data the pipeline cannot
currently reach, finds a working ATS URL or a working job-search page, and
records it in the workbook — **writing nothing it has not proven works**.

### Success criteria

- Every URL written has been driven through a real collector (or rendered)
  and returned jobs.
- A company for which nothing verifiable is found is recorded as
  **not found**, never as a plausible guess. The user finds those manually.
- The hand-curated `ATS URL` and `Live Jobs Page` values are not overwritten
  by default.

### Non-goals

- Replicating judgment about whether a page "looks right". Verification
  replaces judgment.
- Web-search-based discovery. Explicitly rejected: scraping a search engine
  is rate-limited, ToS-grey, and the first thing to break silently.
- Slug guessing (`boards.greenhouse.io/{slug}`). Rejected: weak exactly where
  the remaining failures are (Workday and Taleo need a site name as well as a
  tenant).
- Collectors for newly-seen platforms **Jobvite** (FirstCash) and **Njoyn**
  (CGI) — one company each; not worth a collector.

## Approach

**HTTP first, browser on miss.** A plain GET answers for a meaningful share
of companies in milliseconds; the browser is reserved for JS-rendered sites
that HTTP cannot see. Browser-only would be correct but pays 20-40s for every
company including the easy ones; HTTP-only would miss most of the remaining
failures, which are JS-heavy enterprise sites.

Most of the machinery already exists and is reused rather than rewritten:

| Existing piece | Role here |
|---|---|
| `ats/resolver.py::resolve_from_page` | HTTP fetch, fingerprint, embedded-URL extraction |
| `ats/detector.py::extract_any_embedded_ats_url` | Pull an ATS URL out of page HTML |
| `browser/playwright_scraper.py::_navigate_to_job_list` | Browser crawl that already returns `discovered_ats_url` |
| `ats/router.py::collect_via_api` | The verification step — does this URL actually return jobs? |
| `export_ats_urls.py` | Backed-up, safe workbook write-back |

## Components

### `ats/discovery.py` (new)

Pure logic, no CLI, independently testable.

```python
@dataclass
class Discovery:
    company: str
    ats_url: str | None          # verified, or None
    provider: str | None
    jobs_page: str | None        # verified listings page, or None
    jobs_found: int              # 0 when nothing verified
    method: str                  # "http" | "browser" | "none"
    note: str                    # why it succeeded or failed

def discover(company: str, seed_url: str | None,
             *, use_browser: bool = True) -> Discovery: ...
```

**Seeding.** Start from the sheet's URL when present, and always also derive
the company's root domain from it (`careers.frostbank.com` →
`frostbank.com`). The root domain matters because a marketing careers page
often does not link to the ATS, while the corporate homepage's footer does.

**Stage 1 — HTTP.** Fetch each seed. On every page: run `detect_ats` on the
final URL; run `detect_from_html` plus `extract_any_embedded_ats_url` on the
body; collect careers-ish links using the existing `JOBS_PAGE_HREF_HINTS` and
fetch up to 5 of them, one level deeper. Collect candidates; do not judge.

**Stage 2 — browser.** Only when Stage 1 produced no *verified* result. Runs
`_navigate_to_job_list` from the seed with a larger budget than the runtime
one (this is off the critical path, so `hop_budget_seconds` may be raised via
an override argument rather than the global setting).

### Verification — the core rule

Nothing is written on a pattern match. Two candidate kinds, two proofs:

- **Candidate ATS URL** → construct its collector via
  `ats.router.COLLECTORS[provider]` and call `collect()`. Accept only if it
  returns **≥ 1 job**. A `CollectorUnavailable`, an exception, or zero jobs
  means rejected.
- **Candidate jobs page** (no recognizable ATS) → render it and count rows
  with `_extract_job_rows`. Accept only if it yields **≥ `hop_good_enough_rows`**
  (default 10). The threshold exists because landing pages show a handful of
  "featured" roles; three rows is not a job list.

When neither proof succeeds, `Discovery` carries `ats_url=None`,
`jobs_page=None`, `jobs_found=0`, `method="none"`, and a note naming the last
failure reason. **This is a first-class outcome, not an error** — the user
handles those manually, and a guess would be worse than a blank.

### Write-back policy (`export_ats_urls.py`)

New `write_suggestions(companies_path, discoveries, *, apply=False)`.

Default is **suggest-only**, writing three new columns:

| Column | Contents |
|---|---|
| `Suggested ATS URL` | verified ATS URL, else `NOT FOUND` |
| `Suggested Jobs Page` | verified listings page, else `NOT FOUND` |
| `Discovery Notes` | e.g. `http: oracle cloud via embedded host, 575 jobs` |

Rationale: the user hand-curated all 161 rows immediately before this tool
existed. Silently overwriting that work would be hostile, and a wrong
overwrite is harder to notice than an extra column.

Two exceptions where the tool writes the real columns directly:

1. A **blank** `ATS URL` cell is filled with a verified URL — this is exactly
   the existing `write_discovered_urls` behavior and is already trusted.
2. `--apply` promotes suggestions into `ATS URL` / `Live Jobs Page`,
   overwriting only cells whose company is currently `Data Retrieved = FALSE`
   (a value that is already failing cannot be made worse by a verified one).

The workbook is copied to `companies.xlsx.bak-{timestamp}` before any write,
matching current behavior.

### `tools/find_ats_urls.py` (new)

Thin CLI, same shape as `tools/probe_site.py` and `tools/canary.py`:

```
python tools/find_ats_urls.py                 # all rows lacking a verified path
python tools/find_ats_urls.py --only-failures # only Data Retrieved = FALSE
python tools/find_ats_urls.py --limit 10
python tools/find_ats_urls.py --no-browser    # HTTP stage only, fast
python tools/find_ats_urls.py --apply         # promote suggestions
```

Prints a per-company line (company, method, provider, jobs found) and writes
`output/ats_discovery.csv` with the full `Discovery` records.

### Detector fix (independent, ships with this)

Add `oraclecloud.com` to `_EMBEDDED_URL_PATTERNS[TALEO]` so
`extract_any_embedded_ats_url` surfaces embedded Oracle Cloud API hosts.
Verified to recover Nokia (575 jobs). `HOST_PATTERNS` already maps the host
to `taleo`, and `TaleoCollector` already handles it — only the extraction
pattern is missing.

`selectminds.com` (UT Southwestern) is Taleo-family per its page markers, but
its Oracle Cloud endpoint returned HTTP 404 during design, so **no claim is
made that it is recoverable**; it is left to the tool to verify or report as
not found.

## Error handling

Every stage is best-effort and isolated per company: a crash, timeout or
malformed page yields `method="none"` with the reason in `note`, and the run
continues. The tool never raises out of a single company. Nothing about it
touches `data/jobs.db` or the normal run's outputs.

## Testing

- **Unit** — candidate extraction from fixture HTML (an Oracle CX page
  embedding a `fa-*.oraclecloud.com` host; a marketing page embedding
  nothing); the verify decision (≥1 job accepts, 0 jobs rejects, exception
  rejects); the `NOT FOUND` path produces a `Discovery` with `jobs_found=0`.
- **Write-back** — suggestions land in the new columns; existing `ATS URL`
  and `Live Jobs Page` values are untouched without `--apply`; a backup file
  is created.
- **Live smoke** — Nokia (expect Oracle Cloud, ~575 jobs), Frost Bank (expect
  Workday via embedded host), plus Infosys / TCS / HCLTech (expect a verified
  find *or* a clean `NOT FOUND` — never a marketing URL written as though it
  worked).
- **Regression** — `tools/canary.py` still passes; this tool changes no
  collection path used by a normal run, except the additive detector pattern.

## Out of scope

- Web-search discovery (DuckDuckGo/Bing HTML scraping).
- Slug-guessing candidate URLs.
- Jobvite and Njoyn collectors.
- Automatic invocation during a normal run. Revisit once real-world runtime
  and hit rate are known.
