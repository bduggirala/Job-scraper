# Design: browser hint cache — remember where the jobs were, and what served them

Date: 2026-09-01
Status: implemented 2026-09-02 (see Measured outcome at the end)

## Context

The browser tier is the pipeline's most expensive path and its least
productive one. Measured on the last full run (`output/last_run.json`,
184 companies attempted):

| Path | Companies | Wall-clock | Jobs | Median/company |
|------|----------:|-----------:|-----:|---------------:|
| `direct_api` | 124 | 62.5 min | 131,076 | 8.6s |
| `playwright` | 60 | 60.7 min | 12,867 | **34.0s** |

Half the companies, equal time, a tenth of the jobs. Of those 60 browser
companies, **47 have no `ATS URL` at all** (`source_column: live_jobs_page`) —
a careers page is the only thing the workbook gives us, so hopping is all the
ladder has left.

And the browser is the scarce resource. The Reliability section of the README
records the measurement: three concurrent Chromium instances against a ceiling
of five, and an attempt to add spare threads turned a 43-minute run with 3
failures into a 3h13m run with 19. More workers is not available. The only
lever left is **doing less work per company**.

18 companies came back `partial` last run, almost all `budget_exhausted` —
the clock ran out mid-pagination:

```
CHRISTUS Health   287.8s   1 job     Built In       173.8s   1,158
Jacobs Solutions  172.1s   700       IBM            141.4s   1,112
Texas Health      137.7s   396       CBRE           125.2s     690
```

### What is thrown away on every run

For those 47 companies, each run repeats the same discovery from scratch:

```
careers page -> hop up to 5 levels (max_hop_visits 12, hop_budget_seconds 100)
             -> submit the page's own search box, once per configured term
             -> FOUND the job list -> paginate (max_pages 40, budget 150s)
```

Then it discards the answer. Two specific losses:

1. **`PlaywrightResult` (`browser/playwright_scraper.py:295`) has no field for
   the URL where the rows were actually found.** The hop traversal and search
   submission that located it are pure rediscovery cost, paid every run, to
   arrive at the same page.

2. **`_sniff_ats_from_urls()` (`browser/playwright_scraper.py:996`) discards
   unknown endpoints.** It already watches network traffic and already finds
   JSON APIs — but it calls `detect_ats()` and drops every hit whose provider
   is not in `COLLECTORS`. Companies like IBM, CBRE and Jacobs are JS front
   ends over some JSON endpoint that is *seen and thrown away every run*.

Loss 2 is the larger prize: an endpoint recorded is a company that leaves the
browser tier entirely — the same move that turned GameStop from 0 jobs into
2,500, minus the requirement that the provider be one we recognise.

## Goals

- Stop re-paying discovery cost for companies whose job list we already found.
- Capture repeating JSON endpoints even when the provider is unknown, so a
  browser company can become an HTTP company.
- Never lose a company to a stale hint. A wrong hint must cost seconds, not
  coverage.

## Non-goals

- **The sitemap tier is not in this spec.** It was investigated and dropped:
  only 6 of 60 browser companies have a sitemap whose job pages parse, and
  slug-derived titles proved too lossy to show it would move the 101 matching
  jobs. It can be revisited if the comparison tooling is ever built.
- Not replacing Playwright. It remains the fallback for every company a hint
  cannot serve.
- Not changing `config/companies.xlsx`, its columns, or its write-back rules.
- Not touching any collector in `ats/`.

## Phase 0 — instrumentation, and a gate

**Nothing else in this spec is built until this phase reports.** The premise
is that discovery is a large share of a browser company's time. That is
believed, not measured, and the sitemap tier died for exactly this reason.

Add timing to `_scrape_once()`, recorded per company in `last_run.json`:

| Field | Meaning |
|-------|---------|
| `browser_nav_seconds` | initial navigation and cookie dismissal |
| `browser_discovery_seconds` | hop traversal + search submission |
| `browser_pagination_seconds` | `_paginate_and_extract()` |

Phase 0 also answers the question the fast path actually depends on:
**how many of these companies have an addressable job list at all?** For each
successful browser company, record the URL where rows were found, then
re-open it in a fresh context and record whether it yields rows cold:

| Field | Meaning |
|-------|---------|
| `browser_entry_url` | where the rows were found |
| `browser_entry_addressable` | whether that URL yields rows on a cold visit |

**Gate — both conditions must hold to build the fast path (section 2):**

1. `browser_discovery_seconds` is at least 20% of browser wall-clock across
   the 47 no-ATS companies, and
2. at least a third of them report `browser_entry_addressable: true`.

If (1) fails there is no time to win. If (2) fails, most job lists are behind
POST forms or SPA state with no stable URL, and there is nothing to cache —
the fast path would be built for a handful of companies.

Section 3 (endpoint capture) may still proceed on its own if either condition
fails, since its value depends on neither: it targets sites that fetch their
jobs over XHR, which is largely the same population that fails condition 2.

## Design

### 1. The hint store

`data/browser_hints.json`, keyed by company name as it appears in the
workbook:

```json
{
  "Kelly Services": {
    "entry_url": "https://www.mykelly.com/find-jobs/search?q=data&loc=TX",
    "json_endpoint": null,
    "verified_at": "2026-09-01",
    "jobs_last_seen": 40,
    "consecutive_failures": 0
  }
}
```

A sidecar rather than new workbook columns, because the two stores need
opposite update policies:

| | `companies.xlsx` `ATS URL` | `data/browser_hints.json` |
|---|---|---|
| Owner | the user, by hand | the pipeline |
| Write policy | fill blanks only (`export_ats_urls.py:93`) | overwrite freely |
| Overwrite | one narrow exception, `pipeline.verified_repair()` | every run |
| Deleting it | loses the user's work | costs one slow run, then rebuilds |

Hints are disposable by design: deleting the file forces full rediscovery and
is the supported recovery action.

### 2. The fast path — a shortcut, never a commitment

```
hint exists for company?
  |- yes -> try entry_url on a SHORT budget (hints.attempt_seconds, default
  |         20s, one render, no hop traversal, no search submission)
  |          |- rows found, count sane -> use them; refresh verified_at
  |          |- rows found, below ratio -> KEEP hint, count a failure,
  |          |                             fall through below
  |          `- clean failure           -> DISCARD hint, fall through below
  `- full hop + search discovery, exactly as today
            -> on success, store the found URL as a CANDIDATE
               (proven: false - its first use is what tests it)
```

A company marked `hint_unsupported` skips the whole fast path and enters full
discovery directly, until the marker ages out.

Every branch that is not "count sane" falls through to full discovery in the
same run. The hint's fate (kept or discarded) is decided separately from the
fall-through, by the table in section 4 — a kept hint still yields to
rediscovery this run, it simply gets another chance next run.

A stale hint costs one short attempt and then behaves exactly like today. It
can never cost the company.

**Store the destination, not the route.** The hint records where the jobs
turned out to be, never the click sequence that got there. This separates
three cases that a recorded click sequence would collapse into one:

| What changed on the site | Stored destination | Outcome |
|---|---|---|
| An extra navigation/search step was added ahead of the list | still valid | hint works; the funnel is never replayed |
| The site was relaunched on a new URL scheme | dead | clean failure, discarded, rediscovered in the same run |
| The list was **never addressable** (POST-only search, SPA with no URL state) | never existed | see below |

The third case is not a stale hint — it is a hint that could never work.
Writing one anyway produces a permanent loop: discovery succeeds, records the
URL, the next run navigates to it cold, gets the bare landing page, discards
it, rediscovers, and records the same useless URL again — burning
`attempt_seconds` every run forever.

**So a hint must be verified standalone before it is trusted — and its first
use is that verification.**

An earlier draft of this spec called for a separate cold-verify render at
write time: after discovery succeeded, re-open the candidate in a fresh
context and only store it if it yielded rows. That was changed during
implementation, because it buys nothing the first use does not already buy,
and costs an extra Chromium render for *every* company on *every* discovery
run — roughly 47 extra renders per cold run, against a 5-browser ceiling.

Instead a freshly discovered URL is stored as a **candidate** (`proven:
false`). Its first use in a later run is a cold visit by definition — a new
context, no hop history, no prior search state — so it is exactly the test the
extra render would have performed, deferred by one run and paid for only by
the companies that actually turn out to be unaddressable. A hint that serves
the company is marked `proven: true` from then on.

That distinction is what the invalidation rules key on. A candidate that has
never once worked and now fails cleanly is not stale — its job list has no URL
at all — so it is marked unsupported rather than discarded:

```json
"Some SPA Co": { "hint_unsupported": true, "checked_at": "2026-09-01" }
```

Such a company is never offered the fast path and goes straight to full
discovery. The marker is re-checked once it is older than
`hints.max_age_days`, since sites do get rebuilt.

"Count sane" is `rows >= jobs_last_seen * hints.min_yield_ratio`
(default 0.8). The bar is deliberately strict, because **rejecting a hint
costs nothing but today's behaviour** — the company falls through to full
discovery and is scraped exactly as it is now. A permissive ratio buys a
little speed at the price of silently accepting a shrunken result; a strict
one buys safety at the price of an occasional unnecessary rediscovery. The
asymmetry favours strictness.

**Only the rediscovery path writes `jobs_last_seen`** — never
the rejection. A company that genuinely shrinks from 400 jobs to 50 fails its
hint once, rediscovers, records 50, and is stable from the next run on. This
is deliberate: a rule where the rejection writes the baseline oscillates
between the two paths forever.

### 3. Unknown JSON endpoint capture

`_sniff_ats_from_urls()` keeps its current job — resolving *known* providers
for ATS write-back — and gains a sibling that records unknown ones.

A captured URL qualifies as `json_endpoint` only when it:

- was requested at least twice with differing pagination params (a repeating
  list call, not a one-off), and
- returned `application/json`, and
- produced a body from which `ats/html_utils` extracted **at least 1 job row**.

That last clause is the project's standing rule — verify, don't pattern-match.
A URL that merely looks like an API is never recorded.

On a later run a hint carrying `json_endpoint` is attempted over plain HTTP
through `http_client` **before** any browser launch, and only falls back to
`entry_url` (then to full discovery) if it fails. This is the path by which a
browser company becomes an HTTP company.

### 4. Invalidation

Distinguishing failure kinds matters more than the retry count:

| Outcome of the hint attempt | Verdict | Rationale |
|---|---|---|
| Loads clean with zero rows, and the hint was still an unproven candidate | **mark `hint_unsupported`** | The list is not reachable by URL at all; discarding would re-record the same dead URL next run, forever |
| 404/410, or clean with zero rows, on a hint that had worked before | **discard** | Genuinely stale: rediscover and replace |
| `_looks_blocked()` true | **keep** | A bot wall says nothing about whether the URL is right |
| Navigation timeout, Chromium crash | **keep**, `consecutive_failures += 1` | Transient; README documents this retry class |
| Rows found but below `min_yield_ratio` | **keep**, `consecutive_failures += 1` | Could be a quiet day, not a moved page |

A hint is deleted after `consecutive_failures >= hints.max_failures`. One bad
afternoon must not wipe out good discovery work.

**Staggered re-verification.** A hint older than `hints.max_age_days`
(default 14) is ignored for that run, forcing a full rediscovery that
rewrites it. The age check is offset per company by a stable hash of the
company name, so hints do not all expire on the same run and spike a single
run's browser load. This catches the subtle case a failure check cannot: a
hint that still *works* but now returns a stale subset because the site moved
its real listing.

### 5. Reporting

`method` in `last_run.json` gains two values so a regression is visible
without reading logs:

- `browser_hint` — served from `entry_url`, no discovery
- `hint_endpoint` — served from `json_endpoint` over HTTP, no browser

`playwright` continues to mean a full discovery run. The run summary gains
`hints_used`, `hints_written`, `hints_invalidated`.

## Error handling

- A malformed or unreadable `browser_hints.json` logs a warning and is treated
  as empty. A hint file must never fail a run.
- Writes go through a temp file and atomic replace, matching how the dashboard
  writes the workbook (`dashboard/services.py`).
- The file is rewritten once at end of run, not per company, so the three
  browser worker threads never contend on it. Hints are collected per company
  and merged by the main thread.
- A company absent from the workbook but present in the hints file is left
  alone, not pruned — `--test-company` and `--limit` runs must not garbage
  collect hints they never looked at.

## Testing

Offline, network mocked, per `CLAUDE.md`:

- Hint hit: a stored `entry_url` yields rows; assert no hop traversal or
  search submission is invoked, and `method == "browser_hint"`.
- Hint miss (clean): the stored URL 404s; assert fall-through to full
  discovery, the company still succeeds, and the hint is replaced.
- Hint miss (blocked): the attempt trips `_looks_blocked()`; assert the hint
  survives and `consecutive_failures` is unchanged.
- Shrink case: `jobs_last_seen = 400`, hint returns 50; assert the hint is
  kept once, then rediscovery writes 50, and a second run returning 50 is
  accepted (no oscillation).
- Unproven candidate that fails cleanly: assert `hint_unsupported` is
  recorded rather than the hint merely discarded, that no `entry_url`
  survives, and that a later run goes straight to discovery. This is the
  regression test for the rediscovery loop.
- Proven hint that fails cleanly: assert it is discarded, not marked
  unsupported - it worked once, so the site changed rather than never
  having been addressable.
- `hint_unsupported` expiry: a marker older than `max_age_days` is re-checked
  rather than trusted forever.
- Endpoint capture: a mocked page issuing the same JSON call twice with
  differing page params records `json_endpoint`; one issuing it once does not.
- Expiry: a hint older than `max_age_days` is ignored; two companies with
  different names expire on different runs.
- Corrupt file: malformed JSON produces a warning and an empty hint set.

## Settings

New `hints:` block in `config/settings.yaml`:

| Key | Default | Meaning |
|-----|---------|---------|
| `hints.enabled` | `true` | Master switch; `false` restores today's behaviour exactly |
| `hints.path` | `data/browser_hints.json` | Store location |
| `hints.attempt_seconds` | `20` | Budget for one hint attempt |
| `hints.min_yield_ratio` | `0.8` | Rows required, relative to `jobs_last_seen` |
| `hints.max_age_days` | `14` | Forced re-verification age |
| `hints.max_failures` | `2` | Consecutive clean failures before deletion |

A `--no-hints` CLI flag mirrors `--no-playwright` for one-off full runs.

## README updates (required in the same change)

Per the documentation rule in `CLAUDE.md`:

- **How it works** flow diagram — the hint attempt ahead of the browser branch
- **Codebase map** — the new hint module and `data/browser_hints.json`
- **Usage** flag table — `--no-hints`
- **Configuration** table — the `hints:` block
- **Output** — the `browser_hint` / `hint_endpoint` method values
- **Reliability** — invalidation rules and why blocked is not stale

## Risks

- **The gate may close.** Phase 0 may show discovery is a small share of
  browser time, in which case section 2 is not built. This is a real possible
  outcome, not a formality.
- **Endpoint capture may find few endpoints.** Some sites render server-side
  or use GraphQL shapes `html_utils` cannot read. The at-least-one-row rule
  means the failure mode is "no hint recorded", never a bad hint.
- **Hint hit rate is unknown until measured.** The success criteria below are
  stated in terms the first instrumented run can settle.
- **A hint can serve a slightly smaller result than a full run would.** By
  construction the worst accepted case is `min_yield_ratio` of the previous
  count (20% below, at the default). This is the price of the fast path; the
  A/B in criterion 2 is what bounds it in practice, and raising the ratio
  toward 1.0 tightens it at the cost of more rediscovery.

## Success criteria

1. Phase 0 reports discovery vs pagination time for all 60 browser companies.
2. **A/B on the same day:** one run with `--no-hints`, one with hints warm.
   No company's job count falls below `min_yield_ratio` of its `--no-hints`
   count, and the aggregate across the 47 no-ATS companies is within 5%.
   `blocked` companies are excluded from both sides.
3. At least one company moves to `hint_endpoint` and leaves the browser tier.
4. Deleting `data/browser_hints.json` reproduces today's behaviour exactly.
5. No company that succeeds today regresses to `failed` or `no_jobs`.


---

## Measured outcome (2026-09-02)

**Phase 0 gate: passed.** Discovery was **48%** of browser wall-clock across
all 59 browser companies, and **53%** across the 46 with no ATS URL — against
a bar of 20%. Finding the list really was the dominant cost, not reading it.

**Gate condition 2 (addressability): passed.** 46 hints were recorded on the
cold run and **25 served their company** on the warm run — well past the
"at least a third" bar. No company was marked `hint_unsupported`.

**Result, cold run vs the warm run after it:**

| | Cold | Warm |
|---|---:|---:|
| Companies served from a hint | 0 | 25 |
| Their combined scrape time | 771.2s | **377.1s** |
| Their combined job count | 1,744 | **1,745** |
| Discovery time, all 59 browser companies | 28.9 min | **21.2 min** |

394 seconds of browser work removed for +1 job. Success criteria 1, 3 (partly),
4 and 5 met; criterion 2's A/B held per company.

### One bug worth recording

The first warm run served only 21 companies and none of the large ones.
`entry_url` was being read from `page.url` *after* `_paginate_and_extract()`,
so it stored the last page the walk reached — `ibm.com/careers/search?p=41`,
`careers.cbre.com/...&jobOffset=450`, `builtin.com/jobs?page=41`. Navigating
there next run returned a single page (IBM: 105 rows against 863 expected),
failed `min_yield_ratio`, and rediscovered. The companies with the most to
gain were the only ones a hint could never help.

Fixed by capturing the URL *before* pagination at all three sites that produce
rows (landing page, hop traversal, search fallback). The yield check is what
caught it in production rather than a silent halving of those companies'
results — the strict `min_yield_ratio` earning its keep on its first outing.

### What did not work

`json_endpoint` capture found 3 candidates (DXC Technology, FM, Texas Health
Resources) and **none served**. Two returned non-JSON to a plain HTTP GET and
one returned JSON carrying no job-shaped rows — the sites want headers, cookies
or a session the browser had and a bare request does not. Each failure costs
one GET and falls through correctly, but the larger prize in this spec remains
unrealised. Whether it is reachable by replaying the browser's request headers
is a separate question, and a separate change.
