# ATS Coverage Expansion — Design

**Date:** 2026-08-21
**Branch:** `ats-coverage-expansion`
**Status:** Approved for implementation

## Problem

When a company's careers page is not recognised as one of the 14 supported
ATS providers, the router falls back to the Playwright browser path, which is
slow (~seconds per page, bounded traversal) and brittle (landing pages, cookie
walls, featured-role teasers) compared with a direct API collector.

A routing diagnostic over the current 161-company workbook shows:

| Stage | Playwright companies |
|-------|----------------------|
| Lexical detection only (`--dry-run`) | 106 |
| After one-GET page resolution (`--dry-run --resolve`) | **42** |

So page resolution already rescues 64 companies. The genuine target set is the
**42 companies** that remain `unknown` after resolution. They are:

Goldman Sachs, Intercontinental Exchange / NYSE Texas, FM, Amazon, Boingo
Wireless, FedEx, Google, IBM, Ericsson, NTT DATA, Infosys, Cognizant,
Capgemini, Deloitte, KPMG, CGI, DXC Technology, Tyler Technologies, American
Airlines, Caterpillar, Equinix, Lockheed Martin, Medical City Healthcare / HCA,
Parkland Health, Conifer Health Solutions, Cardinal Health, Concentra, Texas
Oncology, Addus HomeCare, Aveanna Healthcare, FinThrive, CBRE, Globe Life,
FirstCash Holdings, Intuit, Slalom, Tata Consultancy Services (TCS), Ryan,
CHRISTUS Health, JPS Health Network, Energy Transfer, Kimberly-Clark.

## Non-goals

- **No speculative collectors.** We do not build a collector for an ATS unless
  at least one workbook company runs it *and* it returns real jobs through the
  collector. This preserves the project's core discipline: verification, not
  pattern-matching. (Detection-only entries are exempt — see Prong 1.)
- No change to the normalize → filter → dedupe → SQLite → CSV tail.
- No change to the Playwright traversal itself; it remains the last resort.

## Design

Three prongs plus a diagnostic that sequences them.

### Step 1 — Diagnostic sweep (blocking prerequisite)

A read-only sweep over the unknown set that, per company, does one HTTP GET
(reusing `http_client`) and records:

- every `BODY_FINGERPRINTS` needle that matches (detection gaps for *known* ATS);
- fingerprints for *candidate* platforms not yet in the detector (coverage gaps);
- whether the page carries schema.org `JobPosting` JSON-LD (free wins for Prong 2);
- embedded ATS host references and iframe `src` hosts.

Output: a ranked report grouping the 42 by platform, with a company count per
platform. This is the authoritative worklist that decides which collectors
Prong 3 builds and which fingerprints Prong 1 adds. Built by extending the
existing `tools/find_ats_urls.py` crawling/verification machinery; the report is
written to `output/ats_diagnostic.csv` (or `.md`) and is not part of the
pipeline.

### Prong 2 — Generic JSON-LD `JobPosting` fallback collector

New module `ats/jsonld.py` exposing a `JSONLDCollector` that parses
`<script type="application/ld+json">` blocks and extracts `JobPosting`
entities (`title`, `jobLocation` → location, `url`/`@id` → job_url,
`datePosted`, `employmentType`, `hiringOrganization`). Handles single objects,
arrays, and `@graph` wrappers.

Routing: a new tier in `ats/router.py`, tried **only when provider is UNKNOWN
and before Playwright**. If the JSON-LD collector returns ≥1 job it wins;
otherwise the browser fallback runs unchanged. A detector helper
`page_has_jobposting_jsonld(html)` supports the diagnostic and the router.

Rationale: many career pages (including ones on unknown platforms) embed
JobPosting JSON-LD for SEO. One generic collector converts a slice of the long
tail to a structured path with no per-provider code. Precedent exists — the
iCIMS, SuccessFactors and Avature collectors already parse JSON-LD.

Tests: offline, against fixture HTML containing single/array/@graph JobPosting
shapes and a negative (no JSON-LD) case.

### Prong 1 — Expand the detection catalog

Add host patterns and HTML fingerprints in `ats/detector.py` for platforms not
yet recognised. Two categories:

1. **Known-ATS misses** surfaced by the diagnostic (a supported provider whose
   fingerprint we simply lacked) — these route straight to an existing
   collector.
2. **Detection-only providers** (Jobvite, Workable, BambooHR, JazzHR, ADP,
   Dayforce/Ceridian, Cornerstone/csod, Kenexa-BrassRing, Beamery, Recruitee,
   Teamtailor, Symphony/SmashFly, and any others the diagnostic finds). These
   get a provider constant and fingerprints but **no collector yet**. The router
   already treats a provider that is not in `COLLECTORS` like `unknown`
   (resolve → JSON-LD → browser), so this is safe. The payoff is a live
   coverage dashboard in the dry-run summary and better self-healing telemetry,
   which in turn drives future Prong-3 work.

Detection-only providers are tracked in a separate `DETECTION_ONLY_PROVIDERS`
set so `SUPPORTED_PROVIDERS` (collector-backed) keeps its current meaning.

Tests: fingerprint assertions per new platform, plus a guard that a
detection-only provider does **not** get routed to `COLLECTORS`.

### Prong 3 — Verified collectors for every fetchable platform

For each distinct real ATS the diagnostic finds among the 42 that exposes a
fetchable job endpoint, build a collector following the existing pattern:

- `ats/<provider>.py` implementing the `ATSCollector` interface, raising
  `CollectorUnavailable` when it cannot serve a tenant;
- detector constants + `HOST_PATTERNS`/`BODY_FINGERPRINTS`/`_EMBEDDED_URL_PATTERNS`
  entries, and membership in `SUPPORTED_PROVIDERS`;
- registration in `router.COLLECTORS`;
- offline fixture tests **and** a real-tenant verification (from the 42) that
  returns ≥1 job.

Known likely candidates (to be confirmed by the diagnostic): Amazon
(`amazon.jobs` public search JSON), Google careers (public jobs API). Others
depend on what the sweep clusters. Any platform that is a pure client-rendered
SPA with no fetchable endpoint and no JSON-LD stays on the Playwright/JSON-LD
path — we do not force a collector where none can be verified.

## Subagent decomposition

1. **Agent A (diagnostic)** — read-only sweep → ranked report. Blocking.
2. **In parallel after A** (B has no dependency on A and may start at once):
   - **Agent B** — `ats/jsonld.py` collector + router tier + offline tests.
   - **Agent C** — detection-catalog expansion (consumes A's known-ATS misses).
   - **Agents D…N** — one per fetchable platform from A's ranking, each with a
     real-tenant verification.
3. **Integration pass** — re-run `--dry-run --resolve`, full `pytest`, and
   `tools/canary.py`; confirm the Playwright-42 count drops and no regression.

Each agent works in isolation on its own files; detector/router are shared
touch-points, integrated and de-conflicted in the final pass.

## Success criteria

- Playwright-fallback count after `--dry-run --resolve` drops materially below
  42 (exact target set by the diagnostic).
- Every new collector returns ≥1 real job from a real workbook company.
- `pytest tests/ -v` and `tools/canary.py` both green.
- Dry-run provider summary shows named platforms in place of `unknown` for the
  detection-only additions.

## Diagnostic outcome (2026-08-21) & revised worklist

The Step-1 sweep (`output/ats_diagnostic.csv`) reshaped the plan:

- **The dominant blocker is a WAF/UA 403 during resolution**, not missing
  fingerprints. The bare `Mozilla/5.0` HTTP UA is 403-blocked on ~9 sites, so
  resolution never sees the fingerprint. This adds **Prong 0**: on a 403 during
  resolution, retry that single GET once with a browser UA — scoped per-request
  so iCIMS keeps the bare UA (README documents that a Chrome UA trips AWS WAF
  captcha on some iCIMS tenants). A test must assert iCIMS resolution still uses
  the bare UA.
- **No company in the 42 emits `JobPosting` JSON-LD.** Prong 2 helps none of
  this cohort; it is still built as future-proofing for later additions, wired
  between resolution and Playwright.

**Prong 3 — collectors to build (fetchable endpoints confirmed):**

| Collector | Verify against | Endpoint hint |
|-----------|----------------|---------------|
| Amazon.jobs | Amazon | `https://www.amazon.jobs/en/search.json` (public JSON, paginate by `offset`/`hits`) |
| Jobvite | FirstCash Holdings | `jobs.jobvite.com/firstcash-holdings-inc/` (per-tenant feed) |
| Cornerstone (CSOD) | JPS Health Network | `{tenant}.csod.com` careersite API (discover tenant) |
| Dayforce (Ceridian) | FinThrive | `{tenant}.dayforcehcm.com/CandidatePortal` JSON |
| Jibe (iCIMS) | Concentra | `concentrahealthservices.jibeapply.com` job search API |

**Detection-gap — existing collectors, need routing fingerprints (no new collector):**
Cardinal Health → Radancy (`jobs.cardinalhealth.com/search-jobs/results`); Parkland
(`jobs.parklandcareers.com`, public `/api/mcp/jobs`) & FedEx → Phenom; Lockheed
Martin → Eightfold; Deloitte (`apply.deloitte.com`) → Avature; Aveanna
(`jobs.aveanna.com`) → Workday; Boingo → Greenhouse (`boards-api.greenhouse.io/v1/boards/boingo`).

**Deferred:** Oracle SelectMinds (Energy Transfer is also Phenom, covered by the
Phenom fix). **Out of scope (stay on Playwright):** custom SPAs and hard-WAF
hosts with no server-side fingerprint and no public endpoint — Goldman, Google,
IBM, HCA, TCS, Kimberly-Clark, Capgemini, ICE, Slalom, Ryan, CHRISTUS, CBRE,
Globe Life, Addus, Texas Oncology, NTT DATA, Ericsson, Cognizant, KPMG, Infosys,
American Airlines. `FM`'s host is dead (NXDOMAIN) — needs a separate live-host
lookup.

**Isolation for parallel build:** each collector agent creates only its own
`ats/<provider>.py` + `tests/test_<provider>.py` and verifies by constructing
the collector directly against the real tenant (as `tests/test_radancy.py`
does). Agents do **not** edit `ats/detector.py` or `ats/router.py`; they return
the exact snippets, which are applied in a single serial integration pass to
avoid merge conflicts on those shared files.

## Implementation outcome (2026-08-21, live-verified)

The integration pass plus live verification against the real workbook tenants
settled the worklist as follows.

**Shipped and verified (return real jobs):**

| Provider | Verified against | Result |
|----------|------------------|--------|
| Amazon.jobs | Amazon | 10,000 jobs (search.json) |
| Jobvite | FirstCash Holdings | 381 jobs |
| Jibe | Concentra tenant | 1,206 jobs |
| Cornerstone (CSOD) | JPS Health tenant | search API works with the balanced-brace token fix; the branded host `www.jobs.jpshealthnet.org` is unreachable from CI, so it is driven by tenant coordinates |
| JSON-LD tier | (future-proofing) | offline fixtures only — no cohort company emits JobPosting |
| **Avature (self-hosted)** | **Deloitte** | **100 jobs** — new `avature.portal` body fingerprint; `apply.deloitte.com` never mentions `avature.net`, so the SPA's `avature.portal` global is the signal. Resolves end-to-end (branded page → avature → direct API). |

**Verified board, workbook data-fix (no server-side fingerprint to detect):**
Boingo Wireless → `https://boards.greenhouse.io/boingo` (7 jobs, DAS roles —
matches the company). Its marketing page (`www.boingo.com/careers/`) is an SPA
that never references greenhouse server-side, so detection cannot recover it;
the verified board URL was written into `config/companies.xlsx` instead.

**Not viable — stays on Playwright/JSON-LD:**

- **Dayforce (FinThrive).** The endpoint was fully reverse-engineered
  (`jobs.dayforcehcm.com/api/geo/{clientNamespace}/jobposting/search`, POST),
  but it is bot-protected: GET → 405, POST → **403 Forbidden** even with a
  bootstrapped session, cookies, Referer/Origin and the JS's payload shape
  (reCAPTCHA site key present in runtimeConfig). No collector shipped — shipping
  one we cannot verify would violate the project's verification discipline.
- **Parkland → Phenom.** Parkland runs Phenom's newer `/api/mcp/jobs` API, not
  the `phApp.ddo` server object the existing PhenomCollector drives; the
  collector raises `CollectorUnavailable`. Would need a new Phenom-MCP path.
- **Cardinal Health → Radancy.** `jobs.cardinalhealth.com/search-jobs/results`
  returns zero jobs (not a TalentBrew endpoint / different shape) — SPA.
- **Aveanna → Workday, Lockheed → Eightfold, FedEx → Phenom.** All
  client-rendered SPAs (or WAF-blocked, Lockheed serves a 160-byte stub) with no
  server-side ATS host or fingerprint in the one-GET HTML.

**Net effect:** Amazon, Jobvite, Jibe, Cornerstone and Deloitte move off
Playwright to direct APIs; Boingo becomes a direct Greenhouse company via the
workbook. The remaining unknowns are genuine client-rendered SPAs / hardened
endpoints that correctly remain on the browser fallback.

## Risks

- **Public APIs change / rate-limit.** Mitigated by `CollectorUnavailable`
  fallback to JSON-LD/browser — a broken collector never fails the company.
- **JSON-LD quality varies.** The generic collector keeps jobs even with a
  missing date (flagged `date_unavailable`, per existing policy) and skips
  malformed entities rather than throwing.
- **Shared-file merge conflicts** across agents on `detector.py`/`router.py` —
  contained by the serial integration pass.
