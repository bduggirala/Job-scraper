# Design: accuracy, speed, resilience, and coverage hardening

Date: 2026-08-21
Status: approved, pending spec review

## Context

The scraper routes 171 DFW companies to 13 direct ATS APIs or a Playwright
browser fallback, filtering for Data Engineer roles in DFW/remote posted
within a configurable freshness window, writing `output/company_jobs.csv`.

This session found and fixed three infrastructure/environment bugs that were
masking the pipeline's real behavior:

1. Playwright's Chromium binaries were installed to a sandboxed path
   invisible to the venv's actual Python process - every browser-fallback
   company was one `Executable doesn't exist` error away from failing.
   Fixed by reinstalling to the path the venv's Python actually resolves.
2. Launching the pipeline with `nohup ... &` on top of the harness's own
   process backgrounding corrupted Playwright's sync API ("Sync API inside
   the asyncio loop"), failing 77 of 89 browser-routed companies in one run.
   Root-caused via a foreground reproduction; fixed by using plain
   backgrounding only.
3. Two real product bugs, diagnosed with live evidence and fixed this
   session, already confirmed working under full concurrency (`run3`):
   - **Wrong-URL reuse** (`ats/router.py`): when the resolver discovers an
     ATS link embedded in a branded careers page, that URL overwrote the
     plan's URL entirely - used for both the failed direct-API attempt *and*
     the browser fallback. For UnitedHealth Group/Optum/USMD, the discovered
     link was Taleo's "My Submissions" login page (`careerSectionUnAvailable:
     true`, confirmed by calling the endpoint directly), so the browser
     fallback rendered a login page and found nothing either. Fixed by
     preserving the original branded-page URL (`RoutePlan.original_url`) and
     using it for browser fallback. Confirmed recovering UnitedHealth Group
     (0->18 jobs), Optum (0->18, including a real Data Engineering match),
     USMD (0->18), plus Baylor Scott & White (0->32), Tenet Healthcare
     (0->4), D.R. Horton (0->25), UT Southwestern (0->10).
   - **WAF-triggering User-Agent** (`config/settings.yaml`): the pipeline's
     `Chrome/122.0.0.0` UA string reproducibly triggers AWS WAF Bot Control's
     captcha challenge (HTTP 405) on at least three iCIMS tenants; a bare
     `Mozilla/5.0` UA gets a clean 200, 6/6 in direct testing. Fixed by
     changing the default UA. Confirmed recovering RealPage (0->81 jobs via
     direct API) and unblocking State Farm's API (still 1 job - it's
     genuinely an events-only portal, not a bug).

With those fixed, the latest full run (`run3`) scored 123/171 companies
(71.9%), 216 target data-engineering jobs, 36 DFW/remote matches, 18 within
the (now 168-hour) freshness window, in **13.6 minutes wall-clock** -
substantially faster than the 35-45 minute estimate this session started
from.

## Goal

The user's stated goal is to eventually get real job data from all 171
companies. This design does not claim to reach 100% - a meaningful minority
of the remaining ~48 failures are companies whose job data is unreachable
without either (a) reverse-engineering a private client-side API per company
(e.g. Salesforce: page is fully fetchable over plain HTTP, but job listings
load via client-side JS/GraphQL after load, not present in server HTML) or
(b) defeating browser-fingerprint-based bot detection that survives
`playwright-stealth` (e.g. Nokia, per prior investigation). Both are
explicitly out of scope: the user chose to "stay lean" rather than add an
LLM extraction fallback or paid anti-bot infrastructure this round.

What this design *does* commit to, ranked by the user's stated priority
(accuracy > speed > resilience > coverage):

1. Fewer jobs miscategorized as `date_unavailable`; malformed records
   flagged, not silently shipped.
2. A concurrency setting backed by this session's real timing data, not a
   guess.
3. Dependencies pinned; the Taleo error-masking bug fixed; a sub-minute
   canary smoke test that would have caught today's regressions immediately.
4. The Workday "incomplete coordinates" cluster (4 companies) fixed; a
   narrow, reversible `curl_cffi` experiment for the remaining WAF-blocked
   hosts.

## 1. Data accuracy

### `dateparser` for date normalization

Add `dateparser` (`requirements.txt`: `dateparser>=1.2`) as the fallback date
parser wherever a raw date string is currently either regex-parsed narrowly
or given up on. Concretely, in `normalize.py` (wherever `date_posted` is
normalized before `filters.py` computes `date_filter_status`): try the
existing fast-path parser first (cheap, handles the common ISO/US-format
cases already seen from direct-API collectors), and only call
`dateparser.parse()` as a fallback for strings that don't match - this keeps
the common case fast and only pays `dateparser`'s heavier parsing cost on
the messy strings it exists for ("3 days ago", "Posted Aug 18", browser-
scraped relative dates). `dateparser.parse()` returns `None` on genuine
failure, which maps to today's existing, correct `date_unavailable` /
"kept and flagged, never invented" behavior - no change to that contract,
just fewer strings that fall through to it.

### Record validation

In `normalize.py::build_record`, add a `record_incomplete: bool` field
(`True` when `title` or `job_url` is missing/empty after normalization).
This is a new column in `output/company_jobs.csv`/`.json` alongside the
existing `date_filter_status` - same pattern, job-level not company-level,
so it sits next to the record it describes rather than in
`scraper_failures.csv` (which is company-level and already means something
different: the company's scrape failed outright). Records are never
dropped for this reason alone, consistent with the project's existing
philosophy of never silently discarding uncertain data - the flag lets
downstream review filter them out if desired.

## 2. Speed

Real measurements from this session's `run3` (fully-fixed, 171 companies,
`playwright_workers: 3`, `http_workers: 10`):

- Total wall-clock: 13.6 minutes (00:49:51 start -> 01:03:28 output written).
- Direct-API phase (81 companies, 10 workers): resolves in the first ~2
  minutes of the run, overlapping with Playwright dispatch.
- Playwright phase (90 companies, 3 workers) is the bottleneck: spans
  roughly 00:51:25 to 01:02:25 (~11 minutes) for 90 companies / 3 workers =
  30 sequential slots/worker => ~22 seconds/company average, consistent
  with `wait_after_load_ms: 2500` plus navigation/extraction/occasional hop.

This is already faster than the 35-45 minute figure this session started
from (likely a conservative estimate from before this session's ATS
write-back and ordering improvements reduced repeated resolution work).
Given accuracy and resilience rank above speed, the recommendation is a
single, bounded, config-only change: raise `concurrency.playwright_workers`
from 3 to 5 (a ~1.7x increase, chosen conservatively - each worker holds a
full headless Chromium instance in memory, and this session's environment
already showed sensitivity to environment/resource issues this session, so
this should be validated with a real full run rather than pushed
aggressively on the first try). `http_workers` stays at 10 - the API phase
is not the bottleneck. No code changes.

## 3. Resilience

### Pin dependencies

`requirements.txt`: `playwright>=1.40` -> `playwright==1.62.0`,
`playwright-stealth>=2.0` -> `playwright-stealth==2.0.3` - the versions
actually installed and confirmed working this session. Floating minimums
mean a future `pip install` can silently pull a new major version with
different behavior (this is exactly the kind of drift that made this
session's environment debugging necessary in the first place, even though
the specific bugs found were environment/launch-method issues rather than
a version bump).

### Fix Taleo's exception-masking bug

`ats/taleo.py::TaleoCollector.collect()`: today, when `_collect_legacy_taleo()`
raises `CollectorUnavailable`, the `except` block calls
`_collect_oracle_cloud()` as a fallback; if *that* also fails, its exception
propagates as the final error - overwriting the original, often more
relevant legacy-search failure reason. This cost real debugging time this
session chasing a misleading "Oracle Cloud API unavailable: HTTP 404" for
tenants that were never Oracle Cloud sites. Fix: catch the ORC fallback's
exception and raise a `CollectorUnavailable` that includes *both* messages
(legacy Taleo failure first, ORC fallback failure second), so the real
signal (e.g. `careerSectionUnAvailable: true`) is visible in
`scraper_failures.csv` without needing to re-diagnose by hand.

### Canary smoke test

New `tools/canary.py`: a thin wrapper that runs `--test-company` against a
fixed list of ~8 companies chosen to span the major providers actually seen
in production runs (one each: Workday, Greenhouse or Lever, Taleo, iCIMS,
Phenom, SuccessFactors, Eightfold, one Playwright/unknown-provider company),
and asserts each returns more than zero jobs. Exits non-zero with a clear
per-company report if any provider-class is broken. Not part of the pipeline
proper (same category as `tools/probe_site.py`) - a pre-flight check to run
before trusting a full 13-40 minute run, and specifically would have caught
today's browser-path regression (89 companies failing) in under a minute
instead of costing a full wasted run.

## 4. Coverage

### Workday "incomplete coordinates" cluster

Four companies (USAA, Frost Bank, HCSC, Abbott Laboratories) fail direct-API
routing with an identical `Incomplete Workday coordinates (host=..., tenant=
None, site=None)` message - the URL-to-tenant/site parsing in
`ats/detector.py`'s Workday branch (or `ats/workday.py`, to be confirmed
during implementation) doesn't handle these hosts' shape (custom vanity
domains like `www.usaajobs.com`, `careers.frostbank.com` rather than the
standard `{tenant}.wdX.myworkdayjobs.com`). Same diagnostic approach as the
Taleo/UA fixes this session: reproduce directly against each host, find the
actual tenant/site values (likely discoverable via `resolve_from_page`
finding an embedded `myworkdayjobs.com` reference, similar to how Taleo
links get discovered), and fix the extraction gap. Scoped to investigation +
fix during implementation, not pre-diagnosed to the same depth as fixes A/B
were before implementation.

### Narrow `curl_cffi` evaluation

For the WAF-blocked cluster not resolved by the UA change alone (to be
confirmed against `run3`'s failure list at implementation time - the UA fix
already recovered RealPage and unblocked State Farm's API, so the remaining
list may be small or empty): add `curl_cffi` (TLS/JA3 fingerprint
impersonation) as an *optional* per-host transport in `http_client.py`,
selected only for hosts already confirmed WAF-blocked, not a default swap
for `requests`. If a host doesn't clearly improve with `curl_cffi` over the
plain-UA fix already in place, it's left as a known failure rather than
forcing the dependency in. This keeps the blast radius bounded to a handful
of already-broken hosts - every other collector's tested, working behavior
via `requests` is untouched.

## Testing / verification

- `dateparser` and record validation: unit-style check against a sample of
  real `date_posted` strings already sitting in `output/company_jobs_raw.csv`
  from this session's runs (mix of ISO, relative, and browser-scraped
  formats) - confirm `date_unavailable` count drops without any date being
  mis-parsed (spot-check a sample against the source page).
- `playwright_workers: 5`: one full run, compare wall-clock and company
  success count against `run3`'s baseline (123/171, 13.6 min) - success
  count must not regress from the concurrency increase (e.g. resource
  contention causing new timeouts) before this is kept.
- Taleo exception fix: re-run `--test-company` against UnitedHealth Group
  (or any Taleo tenant) and confirm `scraper_failures.csv` now shows both
  the legacy-search and ORC-fallback reasons for tenants where both fail.
- Canary: run before and after a deliberate one-line regression (e.g.
  temporarily reintroduce the `original_url` bug) to confirm it actually
  catches provider-class breakage in under a minute.
- Workday cluster: `--test-company` for each of the 4 affected companies,
  confirm non-zero jobs.
- `curl_cffi` experiment: `--test-company` against the confirmed-remaining
  WAF-blocked hosts, direct-API success/fail before and after, in isolation
  from the rest of the pipeline (no changes to default transport unless a
  host is added to an explicit allowlist).
- Full regression: one more full run after all changes land, compare
  against `run3`'s 123/171, 216 target jobs, 36 DFW/remote, 18 within-window
  baseline - every metric should hold or improve, and no previously-passing
  company should newly fail.

## Out of scope (this round)

- LLM-based extraction fallback for genuinely custom JS-rendered pages
  (Salesforce-class) - user chose to stay lean.
- Paid anti-bot infrastructure (residential proxies, commercial unblocking
  services) for browser-fingerprint-blocked sites (Nokia-class).
- Wholesale `http_client.py` transport replacement - `curl_cffi` stays
  scoped to a confirmed-blocked allowlist.
- Per-company custom scraping handlers.
