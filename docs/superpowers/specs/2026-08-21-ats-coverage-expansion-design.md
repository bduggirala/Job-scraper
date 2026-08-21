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

## Risks

- **Public APIs change / rate-limit.** Mitigated by `CollectorUnavailable`
  fallback to JSON-LD/browser — a broken collector never fails the company.
- **JSON-LD quality varies.** The generic collector keeps jobs even with a
  missing date (flagged `date_unavailable`, per existing policy) and skips
  malformed entities rather than throwing.
- **Shared-file merge conflicts** across agents on `detector.py`/`router.py` —
  contained by the serial integration pass.
