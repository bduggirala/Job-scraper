# Design & plan archive

One entry point for the design specs and implementation plans behind this
scraper. Each document records the *why* behind a body of work that is now
merged; the code is the source of truth, these explain the decisions.

## Current architecture, in one paragraph

The pipeline reads a company workbook, and for each company walks a ladder of
increasingly expensive attempts, stopping at the first that yields jobs:
(1) lexical ATS detection on the URL → direct API collector; (2) one HTTP GET
to resolve a branded page to an embedded/redirected ATS; (3) three generic
single-GET tiers in ascending cost — schema.org JobPosting (JSON-LD), a
server-rendered job list, and a `__NEXT_DATA__`/Nuxt hydration payload;
(4) a Playwright fallback that hops through the careers site and submits its
search box once per configured term, and — if it sniffs out a real ATS behind a
custom page — hands that back to the collector and writes the verified URL into
the workbook. Every paginating collector walks its pages through the one shared
controller in `ats/pagination.py`. Everything after job collection
(normalize → filter for role + location + freshness → dedupe → SQLite →
CSV/XLSX/JSON → optional email digest) is a fixed tail. See `ats/router.py` for
the ladder and `pipeline.py` for orchestration.

## Documents

| Document | Workstream | Status |
|----------|-----------|--------|
| [specs/…-ats-coverage-expansion-design.md](specs/2026-08-21-ats-coverage-expansion-design.md) | Detection catalog + JSON-LD tier + verified per-provider collectors (Amazon, Jobvite, Cornerstone, Jibe) | Implemented |
| [specs/…-ats-discovery-tool-design.md](specs/2026-08-21-ats-discovery-tool-design.md) | On-demand ATS-URL discovery / careers-page repair (`tools/find_ats_urls.py`, `ats/discovery.py`) | Implemented |
| [specs/…-scraper-accuracy-speed-resilience-coverage-design.md](specs/2026-08-21-scraper-accuracy-speed-resilience-coverage-design.md) | Accuracy / speed / resilience / coverage hardening of the existing pipeline | Implemented |
| [plans/…-ats-discovery-tool.md](plans/2026-08-21-ats-discovery-tool.md) | Step-by-step execution plan for the discovery tool | Executed |
| [plans/…-coverage-accuracy-hardening.md](plans/2026-08-21-coverage-accuracy-hardening.md) | Step-by-step execution plan for the hardening work | Executed |

The `specs/` describe design intent; the `plans/` are the task-by-task
checklists used to build them. Both are retained for rationale — the plans are
spent as checklists but still document the concrete decisions made along the way.
