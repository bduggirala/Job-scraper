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
      ▼  still nothing                                                  │
  static HTML ──▶ job-link patterns in the served markup? ──────────────┤
      │                                                                 │
      ▼  still nothing                                                  │
  framework data ─▶ __NEXT_DATA__ / Nuxt / Angular state? ──────────────┤
      │                                                                 │
      │  still nothing                                                  │
      ▼                                                                 │
  hint? ──▶ remembered JSON endpoint? ─▶ plain HTTP, no browser ────────┤
      │  ──▶ remembered job-list URL?  ─▶ open it directly (20s) ───────┤
      │      (either falls through below if it does not pan out)        │
      ▼                                                                 │
  Playwright ──▶ extract job links                                      │
      ├──▶ nothing? hop to "Search jobs" / "View openings" page         │
      ├──▶ nothing? submit the page's own search box, one query per     │
      │             configured term ("Data", "Engineer", "Analytics",   │
      │             "ETL"), merging and de-duplicating the results      │
      ├──▶ walk every result page (next / load-more / infinite scroll)  │
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
                    SQLite tracking (new / changed / removed)
                                        ▼
        output/company_jobs.{csv,xlsx,json} + last_run.json
                                        ▼
                     email digest (only if EMAIL_ENABLED)
```

The search terms stay deliberately broad. On a site whose jobs only exist
behind its own search, that search is the *only* view of its postings we ever
get — so a narrow query is a coverage ceiling, not a filter. A "Snowflake
Engineer" or "ETL Developer" is invisible to a "Data Engineer" query, and
`target_role_patterns` can only ever filter what was actually fetched.

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

### Remembering where the jobs were

The self-healing above solves "which ATS is behind this page". A second
problem sits next to it: for the companies with **no ATS at all**, every run
re-derives the same answer to "where on this site is the job list", by hopping
up to five levels and submitting the site's own search box once per configured
term — then throws that answer away.

`browser_hints.py` writes it down. Two things are remembered per company, in
`data/browser_hints.json`:

- **`entry_url`** — the page the winning rows actually came from. The
  *destination*, never the route. A site that adds a navigation step ahead of
  its job list does not invalidate a stored destination, because the
  intermediate steps are never replayed.
- **`json_endpoint`** — a repeating JSON list call seen in network traffic,
  kept even when `detect_ats` does not recognise the provider.
  `_sniff_ats_from_urls` deliberately keeps only *known* providers, so a custom
  career site's own list API was seen and discarded on every run. A company
  with one of these leaves the browser tier entirely.

**A hint is a shortcut, never a commitment.** Every path falls through to full
discovery in the same run when a hint does not pan out, so a stale hint costs
one short attempt (`hints.attempt_seconds`) and never a company. Deleting
`data/browser_hints.json` forces full rediscovery and is the supported way to
start over.

What a failed attempt *means* matters more than that it failed:

| Outcome | Verdict | Why |
|---------|---------|-----|
| Loads clean, no rows, and the hint had never served this company | mark `hint_unsupported` | The job list is not reachable by URL at all (POST-only search, or an SPA that keeps search state out of the address bar). Merely discarding it would re-record the same dead URL on the next successful discovery and burn the budget on it every run, forever |
| Loads clean, no rows, hint had worked before | discard | Genuinely stale; rediscover and replace |
| Bot challenge (`_looks_blocked`) | keep | A wall says nothing about whether the URL is right |
| Navigation timeout / crash | keep, count it | The transient class documented under [Reliability](#reliability) |
| Rows found but under `min_yield_ratio` | keep, count it | Could be a quiet day, not a moved page |

`min_yield_ratio` is strict (0.8) on purpose: **rejecting a hint costs nothing
but today's behaviour**, since the company simply falls through and is scraped
as it always was, while accepting a shrunken result silently loses jobs.

`jobs_last_seen` is written *only* by a run that actually collected the
company, never by a rejection. That asymmetry is what stops a company from
oscillating between the two paths: one that genuinely shrinks from 400 jobs to
50 fails its hint once, is rediscovered, records 50, and is stable from then
on.

Hints expire after `hints.max_age_days`, staggered per company by a hash of
its name — without the stagger, every hint written on the same first run would
expire on the same later run and spike it back to a full cold discovery for
every company at once.

**Measured, cold run against the warm run that followed it** (2026-09-02, 184
companies, same machine, same day):

| | Cold | Warm |
|---|---:|---:|
| Companies served from a hint | 0 | **25** |
| Their combined scrape time | 771.2s | **377.1s** |
| Their combined job count | 1,744 | 1,745 |
| Browser-tier discovery time, all 59 | 28.9 min | **21.2 min** |

**394 seconds (6.6 min) of browser work removed, for +1 job.** Per company the
wins are large where discovery was the cost — Insight Global 117.9s → 8.3s,
American Airlines 90.4s → 22.3s, Tyler Technologies 82.3s → 17.5s — and
roughly neutral where it was not; a handful of companies come out 2-9s slower,
which is the hint attempt itself on sites that were already cheap to discover.

Two caveats worth knowing:

- **Total run time moves much less than that** (61.2 → 59.4 min of browser-tier
  wall-clock, 3%). Browser companies vary run to run by more than hints save —
  the same variance the [Reliability](#reliability) section documents. The
  per-company comparison above is the honest measure; the aggregate is noise-dominated.
- **`json_endpoint` is unproven in practice.** Endpoint capture found 3
  candidates (DXC Technology, FM, Texas Health Resources) and none of them
  served: two returned non-JSON to a plain HTTP call, one returned JSON with no
  job-shaped rows in it. The failures cost one GET each and fall through
  correctly, but no company has yet left the browser tier this way.

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
| `streamlit run dashboard/app.py` | **Local browser dashboard.** Two tabs: start a run (it shells out to `python main.py --no-email`, or `--retry-failed` for the troubled companies only) and watch it, or add/edit a company. Reads the same `last_run.json` and `company_jobs.csv` every other front door writes. See [Dashboard](#dashboard). | via `main.py` | one appended/edited row, atomically |

**Do I need the discovery tool?** No — `main.py` already discovers and back-fills
ATS URLs on its own. `find_ats_urls.py` is an optional *pre-pass*: bulk-fill or
repair the URL column up front (especially companies stuck on the slow Playwright
path), review suggestions before trusting them, and get an explicit `NOT FOUND`
list to fix by hand — all without a full ~34-minute scrape. Typical loop: run
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
| `--retry-failed` | Re-run only the companies `output/last_run.json` recorded as `failed` or `partial`, merging the results back into the full export and report per company. Blocked companies are skipped — a site that issued a challenge is not fixed by asking again. |
| `--limit N` | Process only the first N companies |
| `--no-playwright` | Disable browser fallback |
| `--no-hints` | Ignore remembered job-list URLs and endpoints; rediscover every browser company from its careers page |
| `--no-resolve` | Skip page resolution and URL repair |
| `--no-write-back` | Don't write discovered ATS URLs into the workbook |
| `--no-email` | Don't send the email digest, even on a full run |
| `--save-raw` | Also write every collected job, pre-filter |
| `--quiet` | Log to file only |
| `--config PATH` / `--excel PATH` | Override config or input workbook |

Partial runs (`--test-company`, `--test-provider`, `--limit`) write to
`test_`-prefixed output files and never modify the workbook, so they cannot
clobber a full run's results.

`--retry-failed` is the exception, because it is the one partial run that knows
exactly which companies it stands for. Its rows are **merged into the full
export and the full run report, per company** — same `company_jobs.csv`,
`company_jobs.xlsx` and `last_run.json` every other reader already opens. See
[Merging a retry back in](#merging-a-retry-back-in) for the rule that decides
what a retried company replaces, adds, or leaves alone. (Narrowing a retry with
`--test-company` makes it an ordinary test slice again, prefix and all.)

---

## Dashboard

A small local Streamlit page over the pipeline that already exists. It starts
the same `python main.py` a terminal would, and reads the same
`output/last_run.json`, `output/company_jobs.csv` and `logs/scraper.log` that
run writes. **No scraper logic lives in the dashboard** — it is a front door,
like `tools/canary.py` or `find_ats_urls.py`.

### Install and start

Streamlit is an *optional* dependency, kept out of `requirements.txt` for the
same reason the test runner is: a deployment that only scrapes should not
install a web server it never starts.

```bash
pip install -r requirements-dashboard.txt
```

```bash
streamlit run dashboard/app.py
```

Then open **<http://localhost:8501>** (Streamlit also prints the URL, and opens
a browser tab itself unless `--server.headless true` is passed). To use another
port:

```bash
streamlit run dashboard/app.py --server.port 8502
```

### Bookmarking it, and the terminal question

**Yes, bookmark `http://localhost:8501/`** — the URL is stable and never
changes. But it is not a website: it is a page served by a program on this
machine, so **the bookmark only works while that program is running.** Open it
with nothing running and the browser says it cannot connect.

What must stay running is the *server*, not a terminal window. `streamlit run`
happens to tie the two together, so closing that terminal stops the server.
Double-click this instead and the terminal goes away:

```text
dashboard\start_dashboard.bat
```

It launches the server with `pythonw.exe` (the windowless interpreter, so no
console is created at all) and opens the browser at the bookmark. Close the
browser, reopen it from the bookmark, come back tomorrow — it keeps serving
until you stop it or the machine restarts.

**A bookmark cannot start the server.** Browsers deliberately refuse to launch
a local program from a bookmark or a link — that restriction is the whole
reason drive-by downloads cannot run themselves, and it is not something to
work around. A `file:///…/start_dashboard.bat` bookmark does not run the file;
Chrome blocks it outright. So use a **shortcut instead of a bookmark**: it both
starts the server and opens the page, which a bookmark can never do. Run this
once to create one on your Desktop:

```text
dashboard\create_desktop_shortcut.bat
```

Then double-click **Company ATS Dashboard** on the Desktop; right-click it to
"Pin to Start" or "Pin to taskbar" if you want it there. Keep the browser
bookmark too — it is the quickest way back to the page *while the server is
already running*.

To stop it:

```text
dashboard\stop_dashboard.bat
```

That stops only the server on port 8501. **A scraper run already in flight is a
separate process and is deliberately left alone** — closing the dashboard must
never abandon a 40-minute run half-way. It carries on writing
`logs/scraper.log`, `output/company_jobs.csv` and `output/last_run.json`, and
the next time you open the dashboard it reports how that run ended, because the
outcome is recorded on disk rather than held in the page.

Two things the dashboard is *not*, and does not become by being bookmarked: it
does not start with Windows (nothing is installed or registered), and it is not
reachable from another machine — Streamlit binds localhost, and the page has no
authentication precisely because nothing but this machine can reach it.

### "Connection error — Is Streamlit still running?"

This banner is the page telling you the truth: the browser tab is open but the
**server behind it is not running**, so it has nothing to talk to. It is not an
error in the dashboard or in the scraper.

It appears whenever the server goes away with a tab still open — you ran
`dashboard\stop_dashboard.bat`, you closed the terminal `streamlit run` was
using, the machine slept or restarted, or you opened the bookmark before
starting anything. The fix is always the same: start the server again, then
reload the tab.

```text
dashboard\start_dashboard.bat
```

A scraper run is unaffected either way — it is a separate process that keeps
writing `logs/scraper.log` and `output/` whether or not the page is up, which
is why the dashboard can tell you how it ended long after the fact.

### The toolbar: Deploy and Rerun are hidden

`.streamlit/config.toml` sets `client.toolbarMode = "viewer"`, which removes
Streamlit's built-in developer options from the toolbar and the ⋮ menu.

**Deploy** is the one that matters. It publishes an app to Streamlit Community
Cloud, which is the wrong action here in three separate ways: the cloud has
none of this machine's files (the workbook, `data/jobs.db`, `output/`,
`logs/`), it could not run the scraper (no venv, no Playwright browsers), and
the page has no authentication *because* only localhost can reach it — a public
URL would let anyone with the link edit the workbook and start runs.

That setting also hides **Rerun** and **Clear cache**, because Streamlit 1.62
has no switch for the Deploy button alone. Neither is a loss:

- The page has its own **Refresh** button, on both tabs, which re-reads the
  workbook, the run report, the export and the log and redraws. It is the same
  capability under a name that says what it does — "Rerun" left people
  reasonably unsure whether it would start a scrape. (It would not: a Streamlit
  button reports a click only on the rerun that immediately follows it, and
  `start_run` refuses anyway while the lock is held. Neither Refresh nor a
  browser reload can start, stop or disturb a run.)
- **Clear cache** was never needed: the cached file reads are keyed on each
  file's modification time, so a changed file invalidates itself. Refresh
  clears the cache too, so the button covers that case as well.

The ⋮ menu keeps its viewer options — light/dark theme, print, screencast,
about. The theme is per browser and affects nothing but the colours.

### Tab 1 — Run Scraper

Press **Run Scraper**. It launches, in a separate process:

```bash
python main.py --no-email
```

`--no-email` is not optional and not a checkbox — a dashboard run never sends a
real digest. `EMAIL_ENABLED=false` is also forced into the child's environment,
which wins because `settings.load_env_file` never overrides a variable that is
already set (see [Notifications](#notifications)). Ticking **Dry run** adds
`--dry-run`: routing decisions only, nothing scraped, no output file rewritten.

While the run is in flight the page shows the run's own log, the company
currently being routed for collection, and an `n of N` bar — all parsed out of
`logs/scraper.log`, so nothing in the pipeline had to change to make progress
visible. The panel refreshes every 3 seconds as a Streamlit fragment, so the
rest of the page stays interactive.

Afterwards it shows, from `output/last_run.json` and the current export:

| Field | Source |
|-------|--------|
| Status: `idle` / `running` / `completed` / `partial` / `failed` | the run lock, then the **exit code**, then `status_counts` |
| Last run started / ended, run duration | the launch record, falling back to the `run_id` (which *is* the start time) |
| Last successful completion | `generated_at` in `last_run.json` — written only when the pipeline reaches the end |
| Output freshness ("Updated 12 minutes ago") | mtime of `output/company_jobs.csv` |
| Companies attempted / successful / partial / failed / blocked | `companies_attempted`, `status_counts` |
| Total jobs collected, matching jobs, removed | `totals` |
| New / changed / unchanged | `change_status` in the current export |
| Within the freshness window | `date_filter_status == within_window` (the window is `hours_old`, 168 h) |
| Run ID, final output path | `run_id`, `config/settings.yaml` |
| Exit code and the console tail, on failure | the launch record and `logs/dashboard_run.log` |

**A run is never called successful because a process started.** The verdict is
the scraper's exit code first; only a clean exit is then refined into
`completed` or `partial` by the run report's own five per-company statuses.

Below that: the current export as a filterable table (company, title, location,
posted date, clickable application URL, provider, extraction method, job
status), the partial/failed/blocked companies with their stop reason or error,
and download buttons for `output/company_jobs.csv`, `output/company_jobs.xlsx`
and `output/scraper_failures.csv` — **the files the run itself wrote**, byte for
byte. The dashboard generates no export format of its own.

#### Re-running only the companies that need it

Under the **Companies needing attention** table is a second button, which
launches:

```bash
python main.py --no-email --retry-failed
```

It re-attempts only the `partial` and `failed` companies from
`output/last_run.json` — a full re-run of 183 companies to fix 21 of them is
most of an hour spent on companies that already worked. The button names the
count it will attempt, and an expander lists the companies by name before
anything is launched.

Three things it deliberately does *not* do:

- **`blocked` companies are not retried.** They appear in the table but not in
  the button's count: the site issued a challenge, and asking again is not a
  fix (see `pipeline.RETRYABLE_STATUSES`). Where the table shows 23 rows, the
  button offers 21.
- **It does not build its own list.** The names come from
  `pipeline.retryable_from_report`, the same function `--retry-failed` itself
  calls, over the same report the table above is drawn from — so the button
  cannot promise a set the run would not attempt.
- **Its results land in the same files this page already shows.** The rows are
  merged into `output/company_jobs.*` and `output/last_run.json` per company —
  see [Merging a retry back in](#merging-a-retry-back-in) — so a company the
  retry fixed drops out of the attention table, its jobs appear in the table
  above, and there is no second file to open. A caption above the button names
  the last retry, since the merged report is otherwise indistinguishable from
  a full run's.

**Dry run does not apply to it.** `--dry-run` routes to the routing report,
which reads the whole workbook, so the retry button ignores the checkbox and
always runs for real.

Only one run may be active at a time, across every browser tab *and* every
dashboard process on this machine — a retry is a run like any other, so it is
disabled while one is in flight and refused if it races another (see
[Concurrency and recovery](#concurrency-and-recovery)).

### Tab 2 — Manage Companies

Shows `config/companies.xlsx` with every column it actually has — `Company`,
`ATS URL`, `Live Jobs Page (if ATS URL unavailable)`, plus `Data Retrieved`,
`Jobs Found` and the `Suggested…` columns other tools write.

**Add a company** takes a name (required) and the two URL columns. Leaving
`ATS URL` blank is the normal case — the pipeline discovers and back-fills it
during a run. Before anything is written:

- the name is normalised exactly as `pipeline.load_companies` normalises it;
- URLs must be `http(s)` with a real host;
- a duplicate name (case-insensitive) is **refused** — the name is the key the
  run report, the job ids and the workbook write-back all match on;
- a duplicate URL, compared after normalisation (case, `www.`, trailing slash
  and fragment removed) and across *both* URL columns, is refused unless you
  tick **Add anyway**;
- a value starting `=`, `+`, `-`, `@`, tab or CR is prefixed with `'`, the same
  guard the pipeline applies to scraped text before writing a CSV.

The write itself: take an exclusive lock file → edit the workbook in place with
openpyxl (so sheets, column widths, the defined table, unrelated rows and every
other column survive) → save to `companies.xlsx.dashboard-tmp.xlsx` → verify it
reopens, has the same header row and the expected row count, **and that
`pipeline.load_companies` can read it** → copy the original to
`companies.bak-dashboard.xlsx` → `os.replace` the temp over the original. The
replace is atomic, so a concurrent reader sees the old file or the new one,
never a half-written one. Exactly one backup is kept, overwritten each time;
it keeps its `.xlsx` extension so recovery is a rename, not a repair.

**Edit a company** changes the two URL columns of an existing row, through the
same path. Renaming is deliberately not offered: the company name is the key
the run report, the SQLite job ids and the write-back all match on, so a rename
here would orphan that row's history. There is likewise **no active/inactive
toggle** — the workbook has no such column and the pipeline scrapes every row,
so a toggle would be a control that quietly did nothing. Remove a company by
deleting its row in Excel.

New and edited rows take effect on the **next** run; the dashboard says so
after each write.

### Concurrency and recovery

| Situation | What happens |
|-----------|--------------|
| Two tabs, or two dashboards, press Run | `output/dashboard_run.lock` is claimed with `O_CREAT｜O_EXCL` *before* anything is spawned, so exactly one wins; the other is told a run is already in progress |
| A run is in progress | Adding and editing are refused — the run reads the workbook *and* writes discovered ATS URLs back into it |
| Streamlit is restarted mid-run | The run keeps going. A supervisor process (`dashboard/runner.py`) holds the lock and records the exit code, so the outcome survives the UI |
| The run crashes, or the machine goes down | The lock outlives its owner. The dashboard checks the PID, reports **stale lock**, and offers a **Clear stale run lock** button. It is never cleared silently, and never while the owner is alive |
| The workbook is open in Excel | Detected via Excel's `~$companies.xlsx` owner file *and* a real write-open attempt. The write is refused before anything is touched |

To recover a stale lock without the UI:

```bash
rm output/dashboard_run.lock
```

To recover a workbook Excel has left locked after a crash — check no Excel
window has it open, then:

```bash
rm config/~\$companies.xlsx
```

To restore the workbook from the dashboard's single backup:

```bash
cp config/companies.bak-dashboard.xlsx config/companies.xlsx
```

### Logs and files the dashboard owns

Four files, all single-current and none of them timestamped or accumulated —
the same policy [Logging](#logging) describes:

| File | Contents |
|------|----------|
| `logs/scraper.log` | Written by the run itself; the dashboard only tails it |
| `logs/dashboard_run.log` | The current run's console output, truncated at each start. This is where a scraper that dies before it can log (bad config, import error) says why |
| `output/dashboard_run.lock` | Present only while a run is in flight; holds the supervisor's PID and start time |
| `output/dashboard_last_run.json` | The last launch's outcome: start, finish, exit code, console tail. Overwritten each run — a status file, **not** a history |

There is no dashboard database, no historical run store and no second export
format. `output/last_run.json` and `data/jobs.db` remain the only records of
what a run did.

The same files from a terminal, when the browser is not where you are:

```bash
tail -f logs/scraper.log
```

```bash
cat logs/dashboard_run.log
```

```bash
cat output/dashboard_last_run.json
```

```bash
ls -l output/company_jobs.csv output/company_jobs.xlsx
```

The output's *freshness* is that file's modification time — which is what the
dashboard turns into "Updated 12 minutes ago". The run behind it is named by
`run_id` in `output/last_run.json` and stamped onto every exported row, and
that id **is** the run's start time in UTC (`20260827T202307Z`); `generated_at`
in the same file is when it finished.

---

## Supported ATS providers

| Provider | Method | Notes |
|----------|--------|-------|
| Workday | `/wday/cxs/{tenant}/{site}/jobs` | Enriches multi-location reqs via job detail |
| Greenhouse | `boards-api.greenhouse.io` | Documented public API |
| Lever | `api.lever.co/v0/postings` | Documented public API |
| Ashby | `api.ashbyhq.com/posting-api` | Documented public API |
| SmartRecruiters | `api.smartrecruiters.com` | Documented public API |
| Paylocity | `recruiting.paylocity.com` | Tenant + board GUID from the URL. JSON API first, then the `window.pageData` blob the page ships the whole board in |
| Eightfold | `/api/apply/v2/jobs` | Uses `?domain=` when present |
| UKG Pro | `JobBoardView/LoadSearchResults` | `ultipro.com` and `*.ukg.net` hosts |
| Phenom | `phApp.ddo` on `/search-results` | Page-embedded JSON, not the dead `/widgets` endpoint |
| Oracle Cloud | `recruitingCEJobRequisitions` | Requires `expand=requisitionList`; reports `TotalJobsCount`, which the walk reconciles against |
| Taleo (legacy) | `careersection/rest/jobboard/searchjobs` | POST search. Row fields arrive as a positional `column` array whose order is configured per portal, so parsing is heuristic and any shape mismatch raises `CollectorUnavailable` |
| iCIMS | HTML + JSON-LD | No public API |
| SuccessFactors | HTML + JSON-LD | OData requires tenant auth |
| Avature | HTML + JSON-LD | No public API; self-hosted portals detected via `avature.portal` fingerprint |
| Radancy (TalentBrew) | `/search-jobs/results` JSON fragment | Runs on the company's own domain; detected by HTML fingerprint, not host |
| Amazon | `amazon.jobs/search.json` | Amazon's own careers API, not a third-party ATS |
| Jobvite | `jobs.jobvite.com/{tenant}` | Server-rendered list; tenant is the first path segment |
| Cornerstone (CSOD) | `career-site/v1/search` | Token-gated; JWT lifted from the careersite home page |
| Jibe (iCIMS) | `{tenant}.jibeapply.com/api/jobs` | Public JSON search API |

Beyond these host/fingerprint-matched providers, **three generic tiers** run
over a single HTTP GET each before the browser fallback, in ascending cost:

1. `ats/jsonld.py` — `schema.org/JobPosting` structured data;
2. `ats/static_html.py` — a server-rendered job list;
3. `ats/framework_data.py` — a `__NEXT_DATA__` / `__NUXT__` / `__INITIAL_STATE__`
   hydration payload, **or any other `window.<name> = {…}` assignment on the
   page** (the generic form of the trick `ats/phenom.py` uses).

Tier 3 used to know exactly four `window.*` names, which covers sites built on
a framework convention and nothing else — and "nothing else" is a large share
of enterprise careers sites, which name their payload whatever they like. The
name is no longer the test: any plausible `window.<identifier> = {…}` is a
candidate, and the safety sits in what happens next — the blob must parse as
JSON, and only objects carrying **both** a title-ish and a URL-ish key become
rows, so analytics `dataLayer` blobs, breadcrumbs and product lists yield
nothing. Bounded at `_MAX_CANDIDATES` (25) parses per page with a
`_MIN_BLOB_CHARS` (200) floor on the speculative ones, so a page carrying a
hundred small config assignments cannot make the tier quadratic. Named
framework payloads are still parsed first, and at any size.

Every company one of these answers is a company that never pays for a Chromium
instance. All three are judged against the same `hop_good_enough_rows` floor,
applied once in the router: a thin harvest is kept as a fallback while the
ladder continues, never accepted as a company's whole job list.

Any collector that cannot serve a tenant raises `CollectorUnavailable`, and the
router falls back to the next tier (JSON-LD → static HTML → framework data →
Playwright) rather than failing the company.

The JSON-LD tier applies the same `hop_good_enough_rows` floor the browser
traversal uses: a landing page embedding two or three "featured" roles for SEO
is kept only as a fallback while the ladder continues, never accepted as the
company's job list. It also runs for a *known* provider whose collector just
failed — previously it was gated on an unrecognised provider and skipped
exactly the case where a cheap tier helps most.

---

## Output

`output/company_jobs.csv` and `.json`:

```
company, title, location, date_posted, job_url, apply_url, employment_type,
remote, description, ats_provider, scraping_method, date_filter_status,
date_source, location_match_type, remote_scope, source_query, fit_score, fit_matched,
fit_explanation, first_seen, is_new, change_status, run_id
```

`output/company_jobs.xlsx` carries the same rows and is what the digest
attaches; the CSV is the machine-readable export.

`run_id` (e.g. `20260826T044813Z`) names the run that produced the file. A
spreadsheet that has been copied somewhere else previously identified itself by
filename alone, so yesterday's export and last month's were indistinguishable.

`change_status` is `new`, `changed` or `unchanged`. `new` wins over `changed`:
a job seen for the first time has no previous state to have moved from.
Removed jobs are deliberately **not** a status — this file lists what an
employer is advertising now, and a row for a closed requisition is a link to a
dead page. Removals are counted in the run summary instead.

`remote_scope` is `remote_us`, `remote_restricted`, `remote_non_us`, `hybrid`
or `onsite`. Only `remote_us` counts as a remote match: a role tied to one
non-Texas state ("Remote — must reside in New York") used to satisfy both the
remote token and the US check, since any state name counted as US eligibility.

`source_query` records which search term surfaced a job on a site whose jobs
only exist behind a search. Blank for everything reached directly.

Text columns are written with a leading `'` when the value starts with `=`,
`+`, `-` or `@`. These come verbatim from third-party pages and the output is
meant to be opened in a spreadsheet, where such a value is executed as a
formula.

`date_filter_status` is `within_window`, `older_than_window`, or
`date_unavailable`. Jobs with no reliable posting date are **kept and flagged**,
never silently discarded — browser-scraped pages rarely expose a trustworthy
date, and inventing one would corrupt the freshness filter.

`date_source` says which date that verdict rests on — `posted` (the employer's
own), `first_seen` (ours, standing in), or `none`. The status alone conflated
the first two, and they are different claims: "the employer posted this three
days ago" and "we first saw this three days ago" are the same label but not the
same fact, because a role listed for six months by a company we only started
scraping last week is *first seen* recently. On the run that prompted this, 13
of 31 exported rows rested on `first_seen` and were labelled identically to the
ones backed by a real posting date. The rows are still kept — the freshness
policy is deliberately inclusive — but a reader deciding whether to apply can
now see which kind of row they have.

`output/scraper_failures.csv` — one row per failed company with `error_type`,
`error_message` and `timestamp`.

`output/last_run.json` — one row per company **attempted**, not just the failed
ones: provider, extraction method, jobs collected, the provider's reported
total, `stop_reason`, duration, and `removal_sync_allowed`. That last field is
the one that most needed surfacing — it is the difference between "this
company's removals are real" and "removals were skipped here", and nothing
reported it. The file also carries the `run_id`, `status_counts` and
`method_counts` for the run.

Company status is one of five, deliberately not two:

| Status | Meaning | What it calls for |
|--------|---------|-------------------|
| `success` | Reached, complete, returned jobs | nothing |
| `partial` | Rows are real, coverage is not | a clean re-run (`--retry-failed`) |
| `failed` | Timeout or error | investigation |
| `blocked` | Bot challenge or explicit denial | a different route in, never a workaround |
| `no_jobs` | Read correctly; not hiring | nothing |

### Merging a retry back in

`--retry-failed` re-runs a handful of companies out of the whole workbook. It
used to write `output/retry_company_jobs.*` and `output/retry_last_run.json`,
which kept the full export honest but left two files to reconcile by hand —
while the dashboard, the digest, the database and the workbook all read the
unprefixed one. A company that was fixed by a retry stayed listed as broken,
and its rediscovered jobs sat in a file nothing else opened.

So a retry now writes **into** the full outputs, per company. The rule for what
a retried company does to its own rows is the database's, not a second one
invented for files — `pipeline.removal_sync_allowed()` is the single predicate,
called by `sync_completed_companies()`, the run report and the merge alike:

| The retry came back… | Its rows | Why |
|----------------------|----------|-----|
| succeeded, complete, no collapse | **replace** that company's | the only case where a row disappearing is real news rather than a scrape we failed to read |
| succeeded but `partial`, or a collapse against last run | **added** to that company's, keyed on `job_id` | it never reached the later pages, so what is missing is missing from *our* data, not the employer's — the same reason the database upserts without syncing removals |
| failed, or came back empty | nothing changes | a company we could not reach tells us nothing about the rows it gave us last time |
| was never visited | nothing changes | — |

The report is spliced the same way: the retried companies' rows are replaced,
every other company's is carried through, and `status_counts`,
`companies_attempted` and `totals.jobs_collected` are recomputed from the
merged set. Three things deliberately do **not** move:

- **`run_id` and `generated_at` stay the full run's.** They answer "which run
  saw the whole workbook, and when did it finish", and a 21-company retry did
  not. The retry identifies itself under a `last_retry` block instead, which
  the dashboard shows above the re-run button — otherwise the page would
  display a retry's numbers with nothing saying a retry produced them.
- **Run-scoped deltas** — `new_jobs`, `changed_jobs`, `removed_jobs`,
  `duplicates_removed` — stay as the full run wrote them. They are measured
  against a different baseline in the two runs, so adding them would be
  arithmetic on two different questions.
- **Each exported row keeps the `run_id` that produced it.** A carried-over row
  is not restamped, which is the whole reason the column exists.

`output/scraper_failures.csv` is merged on company name for the same reason:
`blocked` companies are never retried, and a failures file rebuilt from a
retry's own results alone would quietly report them as fixed.

Where there is no previous export to merge into (a first run, a cleared
`output/`), the merge is exactly a normal write — an empty base merges to the
rows the run produced.

---

## Notifications

A full run emails a digest of new and changed matching jobs with
`output/company_jobs.xlsx` attached (the CSV stands in if the workbook could
not be written — a digest with no attachment at all is the worst of the three
outcomes).

**Email is off by default and every value comes from the environment.**
`.env.example` is the tracked template; copy it to `.env` (gitignored) and fill
it in.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EMAIL_ENABLED` | `false` | Master switch. Overrides `notifications.email.enabled` **in both directions**, so a file in git never decides on its own whether a run mails a human. |
| `SCRAPER_EMAIL_TO` | from config | Recipient(s), comma- or semicolon-separated |
| `SCRAPER_SMTP_HOST` | — | e.g. `smtp.gmail.com` |
| `SCRAPER_SMTP_PORT` | `587` | 587 STARTTLS, 465 implicit SSL |
| `SCRAPER_SMTP_USER` | — | The authenticating account |
| `SCRAPER_SMTP_PASSWORD` | — | Gmail: an App Password, not your login |
| `SCRAPER_SMTP_FROM` | `SCRAPER_SMTP_USER` | Envelope sender when it differs |
| `SCRAPER_SMTP_USE_TLS` | `true` | STARTTLS on a non-SSL port |
| `SCRAPER_SMTP_DRY_RUN` | `false` | Render the digest to disk instead of sending |

`.env` is read automatically at startup (`settings.load_env_file`, called from
`load_settings`), so the values `.env.example` documents take effect just by
being in the file — no exporting by hand. Anything already set in the real
environment wins over it, so a scheduled job that sets a variable still
overrides `.env`. The tracked config carries a **placeholder** recipient and
the real one lives in `.env` as `SCRAPER_EMAIL_TO`; before the loader existed
that file was never read, so the digest was addressed to the placeholder.

### When the digest is held back

A run that could not see everything cannot always be trusted to say what is
*new*, so `notify.should_send()` decides. Truncations in
`ats.base.DESCRIBABLE_STOP_REASONS` never suppress it — their shape is known.
The rest (a failed page, a provider contradicting its own reported total) leave
a hole of unknown shape, and the digest is held **only once more than
`UNTRUSTWORTHY_COMPANY_LIMIT` (3) companies** are in that state.

The count matters, not just the reason. Suppressing on the first one treated a
run of 180 companies as unusable because TEKsystems served 122 of the 136 it
reported — 14 jobs at one employer silenced every alert for the whole workbook.
Below the limit the digest goes out and the run summary names each affected
company with its shortfall; above it the pattern is systemic and the run
genuinely does not know what it saw.

The configured recipient is `you@example.com` (`config/settings.yaml`
and `.env.example`).

**Missing credentials never fail a scrape.** With `EMAIL_ENABLED` unset or
false the run logs that delivery is disabled and completes normally; with it
true but credentials absent, the run logs exactly which variables are missing
and skips the send. The spreadsheet on disk is the real deliverable either way.
No credential value is ever logged, and `EmailConfig.__repr__` hides the
password.

The digest body carries the run's own numbers — run id, companies attempted,
and `success` / `partial` / `failed` / `blocked` separately, because 3 new jobs
out of a clean sweep and 3 new jobs out of the 40 companies that didn't time
out are very different runs.

Three guards decide whether anything goes out:

| Guard | Why |
|-------|-----|
| Something new or changed | A channel that mails "0 new jobs" every run is one you stop opening |
| No company stopped short *for an unknown reason* | A `page_failed` run leaves a hole of unknown shape, so what looks new may just be what we reached |
| Not announced before | The `notifications` table records each job once per state |

The middle guard distinguishes the two kinds of incompleteness. A
`budget_exhausted` truncation walks newest-first, so what it missed is the
*oldest* postings and nothing inside a 7-day window sits behind it — the digest
still sends. Treating it as fatal would mean permanent silence for any employer
too large to collect in full, and CVS Health (19,246 postings at ten per
request) is truncated on every run and always will be.

Notification keys are `(job_id, kind)` where `changed` carries a fingerprint of
the job's tracked fields. Under a bare `"changed"` key a posting that moved city
in March and was retitled in June produced **one alert ever** — the second was
filtered out as already announced, permanently. Fingerprinting makes the key
"this job, in this state", so each distinct change is announced once and a
re-run reporting the same change stays silent.

### Dry run

`notifications.email.dry_run: true` — or `SCRAPER_SMTP_DRY_RUN=1`, which needs
no edit to checked-in config — renders the digest to
`output/digest_preview/` (`digest.txt`, `digest.html`, `digest.eml`) instead of
sending it. It needs **no SMTP credentials** and opens no connection, which is
what makes the whole notification path verifiable without mailing a real
person. A dry run deliberately does *not* mark jobs as announced, so the first
real send still includes everything a preview has seen.

Partial runs (`--test-company`, `--test-provider`, `--limit`, `--retry-failed`)
never send: they know nothing about the companies they skipped. A merged retry
writes the unprefixed files, so "is the prefix empty" stopped being the same
question — the gate is `pipeline.speaks_for_whole_workbook()`, which also
governs the workbook write-back for the same reason.

## Job tracking (SQLite)

`data/jobs.db` tracks every job seen, keyed on a **stable job id** derived from
the posting URL rather than the URL itself — retitling a job changes its URL
slug but not its underlying requisition id, so the same job stays the same row.

Ids are **scoped by company**: `{company}:{id}`. The extracted id is only
unique *within* an employer, and `job_id` is the table's primary key, so
without the scope `https://a.com/careers?jobId=55512` and
`https://b.com/apply?jobid=55512` produced the same id and merged two
employers' postings into one row. The company key is normalized (suffixes and
punctuation dropped) so workbook drift — "Acme Inc" one run, "Acme, Inc." the
next — does not orphan every job that company had.

A generically-extracted id carries **no provider label**. `ats_provider` is the
literal string `unknown` for every browser-routed row, so labelling made one
requisition resolve to two identities depending on which route reached it that
run — `taleo:1001` via the API, `unknown:1001` via Playwright. A company that
fell back to the browser therefore re-keyed its whole job list, reported all of
it as new, and aged out the API-keyed copies. Provider-specific strategies
(Workday's `_R12345` requisition suffix and friends) still carry their label,
because those ids are provider-shaped and unambiguous.

**The query string is part of the identity.** Several platforms put the
requisition id there and nothing else distinguishes two postings: UKG serves
`OpportunityDetail?opportunityId=<uuid>`, Taleo `jobdetail.ftl?job=<id>`, Infor
`shorturl.do?key=<id>`. Dropping the query gave every job a company lists the
same key — measured against a real 120,003-row run, GameStop's 5,148 distinct
postings collapsed to one, BAE Systems' 1,858 to one, and 8,427 real postings
were lost across 18 companies. Tracking parameters are stripped by
`normalize.TRACKING_PARAMS` instead, which is narrower and does the job the
blanket drop was reaching for.

That narrower list has the same failure mode in miniature, so an entry only
belongs on it once it is known to be redundant *everywhere*. `gh_jid` was on it
and is not redundant: a Greenhouse board can be configured to point at the
employer's own careers page, and then the parameter is the entire identity.
ISNetworld serves all 18 of its postings as
`isnetworld.com/en/about/careers/jobs?gh_jid=<id>` - stripping it normalized
every one of them to the same URL and `dedupe_records` reported the board as a
single job. Tenants whose `absolute_url` already carries the id in its path
(SoFi, and every board still on `job-boards.greenhouse.io`) were never affected
and keep their existing ids, because `_PROVIDER_STRATEGIES[GREENHOUSE]` reads
the path before anything looks at the query.

`job_identity.JOB_ID_SCHEME_VERSION` records the format. When it changes, the
`jobs` table is cleared on open rather than left holding ids nothing will ever
match again: such rows are never refreshed and never removed (removal only
considers ids the current run produced) while still inflating the "already
known" set. Every job is reported as new once after such a reset, and the log
says so.

Jobs no longer listed by a company are aged out, not deleted on sight. Three
conditions must all hold before a company is synced at all:

| Condition | Why |
|-----------|-----|
| `result.success` | A company we could not reach tells us nothing |
| `result.jobs` | An empty harvest is not evidence every posting closed |
| **`result.complete`** | A scrape that stopped partway through pagination never saw the later pages — those jobs are missing from *our* data, not from the employer's site |

The comparison is scoped per-company via an index (measured at 0.38ms against a
21,000-row table), never a full-table scan.

Even then removal is not immediate: a job absent from one qualifying scrape has
its `misses` counter incremented, and only after `REMOVAL_GRACE_MISSES` (2)
**consecutive** misses is it deleted. Seeing the job again resets the counter.
One missed scrape is usually a flicker — a slow page, a reordered result set, a
briefly unpublished requisition — and deleting on it destroys `first_seen`,
which makes the job look brand new when it returns.

### Collection completeness

`ATSCollector.collect()` returns a **`CollectionResult`**, not a bare list:

```python
CollectionResult(jobs, complete, pages_fetched, reported_total, stop_reason)
```

`complete` is True only when the collector is confident it saw every row the
provider would serve. A failed page, a tripped job budget, a tripped page
ceiling, or a walk that ended short of the reported total all set it False and
name a `stop_reason`:

| `stop_reason` | `complete` | Meaning |
|---------------|-----------|---------|
| `exhausted` | yes | The provider served everything it had |
| `reported_total_reached` | yes | Collected count met the reported total |
| `no_new_rows` / `repeated_page` | depends | The walk ended on a repeat; complete unless a reported total says otherwise |
| `page_failed` | no | A page beyond the first failed — hole of unknown shape |
| `budget_exhausted` | no | `max_jobs_per_company` tripped while rows remained, on a walk that runs newest-first |
| `budget_exhausted_unordered` | no | The same ceiling against a provider that serves by **relevance**, so the gap is of no particular age |
| `freshness_window_reached` | no | Deliberately stopped: a newest-first provider too large to finish had paged past the freshness window |
| `page_ceiling` | no | `pagination.MAX_PAGES` (500) tripped first |
| `short_of_reported_total` | no | The walk ended naturally, but the provider's own total says there was more |
| `more_results_available` | no | A single-GET tier read page one of a list the page itself advertises as longer |

Three of these are recent and all three were silent completeness lies:

- **`budget_exhausted_unordered`.** `budget_exhausted` is treated as a
  *describable* truncation — a run carrying only describable truncations is
  still trusted to say what is new — and the entire justification for that is
  that the walk runs newest-first, so what was missed is only stale postings.
  Phenom was assumed to qualify on the strength of the `s=1` sort parameter its
  search URL carries. It does not: measured directly against the live CVS
  Health tenant, offset 0 returned a posting from 12 June while offset 7,990
  returned one from 24 August, and none of `s=2`, `s=3`, `sortBy`, `keywords`
  or `q` changed the ordering or the total — that endpoint ignores them all. So
  CVS Health and Signify Health were stopping at 8,000 of 18,904 postings with
  the missing 10,900 being an arbitrary slice **by date**, while the digest
  logic treated them as known-stale. Any collector that cannot establish date
  ordering must report this reason instead, and it is deliberately absent from
  `DESCRIBABLE_STOP_REASONS`.

- **`page_ceiling`.** `MAX_PAGES` bounds the loop independently of the job
  budget, but the reason was derived from the budget alone, so running out of
  *pages* reported `exhausted`. On a ten-rows-per-request provider that ceiling
  is 5,000 jobs however high `max_jobs_per_company` is set — CVS Health lists
  19,246, so the company cited below as the reason budget truncation must not
  silence the digest was itself being reported complete.
- **`short_of_reported_total`.** A tenant that reports 5,000 and stops serving
  at 200 has contradicted itself, and believing it deletes 4,800 live postings.
  A shortfall within `TOTAL_RECONCILIATION_TOLERANCE` (2%) is still complete,
  because totals drift while a walk runs. Only `exhausted` is rewritten —
  `repeated_page` and `no_new_rows` describe an observed event that stays true,
  so only their `complete` flag flips.

Checked against three live Workday tenants before adopting this rule:
requesting offset `total - 5` returned exactly 5 rows on Capital One (1,842),
Travelers (346) and Texas Capital Bank (81), so the reported total is accurate
and a large shortfall is evidence of a stall rather than benign over-reporting.

**The single-GET tiers carry it too.** JSON-LD, static HTML, the framework
payload — and `ats/jobvite.py`, which is a provider collector but reads exactly
one URL like they do — each fetch one page and cannot follow a pagination
control, so whether what they hold is the whole list is a question about the
*page*, not about the harvest, and `ats.html_utils.detect_more_results()` asks
it. Four signals count
as evidence of more:

| Signal | Example seen live |
|--------|-------------------|
| A stated results count above what we extracted | Randstad USA: "5,358 jobs" beside 132 readable rows |
| `rel="next"` | the standardised case |
| A pagination widget (class/id `pagination`, `pager`, `load-more`, …) | Apex Systems |
| A link advertising the fuller list ("View all jobs") | UT Southwestern, Energy Transfer |

The last one is **size-gated** (`_TEASER_CEILING`, 30 rows) and the others are
not. A "view all jobs" link is weak evidence — plenty of real job lists carry
one as ordinary navigation — so it counts only while what we hold is small
enough to actually *be* a teaser. Aveanna Healthcare's page is 3,708 real rows
beside a "View All Jobs Near Me" link; ungated, the rule would have thrown that
complete harvest away and sent the company to a browser returning far less.

Any of them marks the harvest `complete=False` with `more_results_available`,
which does three things: the router **refuses to end the ladder there** and
still pays for the browser; if the cheap rows are kept anyway (the browser found
fewer, or crashed) the caveat rides along so removal sync skips the company; and
the company is listed in the run summary with its shortfall.

`more_results_available` counts as a *describable* truncation
(`ats.base.DESCRIBABLE_STOP_REASONS`, the one definition both `notify` and
`pipeline` read), alongside `budget_exhausted` and `page_ceiling`, so it does
not suppress the digest. Its justification differs from theirs: it is not
newest-first, so the gap is not simply "the oldest postings". What makes it safe
is that the gap is **stable** — the same tier fetches the same page every run,
so the rows it does see are a consistent set and a new posting among them is
genuinely new. The failure mode is a miss, never a false alarm. Excluding it
would mean permanent silence for a handful of teaser careers pages that are
incomplete on every run and always will be.

This was not hypothetical. UT Southwestern's careers page shows its ten newest
openings beside a "View all New Jobs" link and carries no pagination markup at
all. The tier read exactly ten rows, cleared the ten-row `hop_good_enough_rows`
floor, and returned them as complete — after which removal sync aged out
everything else, leaving that employer with exactly ten stored jobs. Energy
Transfer went the same way. Clearing the floor says "this looks like a real job
list"; it never said "and it is all of it", and the router now requires both.

**The browser path carries the same contract.** `playwright.max_pages` (40)
cuts a long list short exactly as a job budget does, and
`PlaywrightResult.complete` now reports it. The signal already existed —
`_paginate_and_extract` returned an `exhausted` flag and the scraper logged
"pagination stopped at the N-page cap" — but it was discarded at all three call
sites, so every browser company claimed it had seen the whole list.

This exists because the two cases used to be indistinguishable. A page failing
partway through pagination produced a partial harvest the router reported as a
success, after which the removal sync deleted every job on the pages we never
reached — reading one transient HTTP error as "those postings closed", and
resetting `first_seen` so they were re-reported as new when they came back.

Incomplete companies are listed in the run summary with their shortfall.

**A fourth condition guards removal sync: the harvest must not have collapsed.**
`complete` is the collector's own account of itself, and it is only as good as
the collector's knowledge. A walk that *knows* it stopped short says so. A walk
that never found the list cannot: the browser traversal renders a careers site,
lands on a "featured roles" panel instead of the job list, extracts four rows,
finds no pagination control, and concludes it saw everything there was. Nothing
inside that scrape can tell it otherwise — but the previous run can.

`pipeline.collapsed_against()` compares this run's count for a company against
the **previous run's** count (from `last_run.json`). Below `COLLAPSE_RATIO`
(0.5) of it, removal sync is withheld and the company is listed under
`COLLAPSED` in the run summary. Companies that collected fewer than
`COLLAPSE_FLOOR` (20) jobs last run are exempt — going from 6 postings to 2 is
an ordinary week at a small employer, not a cliff.

Measured live: Caterpillar collected 138 jobs one run and 4 the next, both
reported `complete`, leaving 143 stored postings one miss from deletion.

The comparison is against the previous *run*, deliberately, not the stored row
count. Nothing is deleted while the guard holds, so a database-based comparison
could never clear itself — the guard would be permanent. Comparing against the
last run gives exactly one run of grace: long enough to absorb a transient
traversal miss, short enough that a real, sustained halving is accepted on the
next run and the removals go through.

All 14 paginating collectors return a `CollectionResult`, and all of them get
there through the one walk in `ats/pagination.py` rather than a hand-written
loop each. The four that do not — Greenhouse, Lever, Ashby, Jobvite — return
their entire board in a single response, so there is no pagination for them to
get wrong; they stay on the `CollectionResult.coerce` shim.

`paginate()` adds two things none of the hand-written loops had:

- **per-page retry.** A transient failure on page 12 used to end the walk and
  mark the company incomplete, suppressing removal sync until the next clean
  run. Most such failures succeed on a second attempt, so the walk completes.
- **repeated-page detection.** A tenant that ignores its own paging parameter
  serves page 1 forever; a content hash stops on the first repeat instead of
  parsing and de-duplicating every one.
- **the provider's page size, not ours.** A walk ends when a page comes back
  short — but short *of what*. The `page_size` a collector passes is only its
  assumption; measuring against it ended the walk after page one for any
  provider serving fewer rows than assumed, and called that complete. Probing
  six live iCIMS tenants against the collector's hard-coded `ROWS_PER_PAGE = 20`
  found 13, 20, 20, 21, 50 and 50 rows per page — four of six wrong. The walk
  now learns the real size from page one. The cost is exactly one extra
  request, and only for a company whose *first* page is short.

A first-page failure always propagates so the collector can raise
`CollectorUnavailable` and let the router fall back; later pages are retried
and then tolerated as an incomplete walk.

**All fourteen paginating collectors now use it**, Taleo included. Both of
Taleo's paths (Oracle Cloud Recruiting and the legacy career section) walked by
hand until they were migrated, which left the ten workbook companies routed
there — JPMorgan Chase, Texas Instruments, Honeywell, Oracle, Digital Realty,
Baylor Scott &amp; White Health, Texas Health Resources, Tenet Healthcare, Molina
Healthcare and PlainsCapital Bank — without per-page retry, without
reconciliation against the `TotalJobsCount` that ORC reports on every response,
and without repeated-page detection.

---

## Configuration

Everything tunable lives in `config/settings.yaml`: the freshness window
(`hours_old`), DFW city list, target-role and exclusion regexes, HTTP
timeout/retry/backoff, Playwright behaviour (including stealth and the search
fallback term), and concurrency (`http_workers: 10`, `playwright_workers: 3`).

### Pagination ceilings

`requests.max_jobs_per_company` (default 10,000) bounds how much any collector
will fetch for one company. It is expressed in **jobs, not pages**,
deliberately: a shared *page* budget means a different job ceiling for every
provider, because page sizes differ. The old `max_pages_per_company: 25` meant
250 jobs on Phenom (10/page) and 5,000 on Oracle (200/page) — which silently
truncated 23 companies in a measured run, including eleven Workday tenants that
all returned exactly 500 (20 × 25) and seven Phenom tenants that returned
exactly 250.

Tripping the ceiling is not an error, but it marks the scrape incomplete, which
suppresses removal sync for that company and lists it in the run summary with
its shortfall. (The older `requests.max_pages_per_company` knob is gone: once
Taleo moved onto the shared controller nothing read it, and a config key that
does nothing is worse than no key at all.)

A second, independent ceiling sits in the controller itself:
`ats.pagination.MAX_PAGES` (500) guards a provider that serves one row per page
forever without repeating content. **Whichever ceiling trips first decides the
`stop_reason`** — `budget_exhausted` or `page_ceiling` — and both mark the walk
incomplete. They bind at very different job counts: at 500 rows per request the
job budget trips first, while at ten rows per request the page ceiling caps the
walk at 5,000 jobs no matter how high `max_jobs_per_company` goes. Deriving the
reason from the job budget alone reported the second case as `exhausted`, which
reads as complete.

What survives a truncated walk should be what the freshness window can still
match, so collectors that can be truncated ask for newest-first where the
provider supports it (UKG `postedDateDesc`, Oracle `POSTING_DATES_DESC`,
Amazon `sort=recent`).

**Where the provider does not support it, say so rather than assume it.**
Phenom's search URL carries `s=1`, which was read as a date sort for a long
time and is not one — see `budget_exhausted_unordered` above. A collector whose
ordering cannot be established reports that reason instead, which keeps its
companies out of the set the digest treats as fully understood.

**And where it does support it, stop at the window rather than at a prefix.**
When a tenant is too large to finish, the only question is *which* subset to
keep, and on a newest-first provider there is a much better answer than "the
first N". `paginate(freshness_cutoff=...)` pages until the provider stops
serving anything inside the freshness window and then stops
(`freshness_window_reached`); everything past that point is older than the
window by construction, so nothing the filter would have kept is missed.

Two guards keep it honest. It only engages when `reported_total` already
exceeds `max_jobs` — otherwise it would turn completed scrapes into partial
ones and silently disable their removal sync. And it needs two consecutive
fully-stale pages, because real feeds carry ordering noise (a re-activated
requisition keeps its original date). `requests.freshness_stop_margin_hours`
(48) is added to `hours_old` so the stop lands clear of the boundary rather
than shaving rows the filter would have kept.

CVS Health is what this was built for, and it moved provider as a result. Its
Phenom board served 19,126 postings in relevance order, so its 8,000-row
ceiling hid jobs of every age *including current ones*. Its own `applyUrl`
values point at a Workday tenant (`cvshealth.wd1.myworkdayjobs.com/
CVS_Health_Careers`) which serves posting-date descending — offset 0 "Posted
Today", 6,000 "9 Days Ago", 12,000 "23 Days Ago". Measured end to end: **6,777
jobs in 220s with the oldest 12 days old, against 8,000 in 432s scattered
across 191 days.** Half the time, and no fresh posting left behind.

**Workday CXS ignores a sort parameter** — verified directly: requesting
`sortBy=POSTING_DATES_DESC` returns a byte-identical first page to sending
nothing, so the collector does not send one rather than carry a dead parameter
that reads as a guarantee. Its default order is already posting-date
descending, measured across Capital One's 1,854 postings: mean age climbs
monotonically from 1.6 days in the first 200 rows to 29.0 days in the last 200.

Target roles match on any title segment naming "data" as its own word (titles
are split on `,`, `/`, `-`, `|`) — `Software Engineer, Data Engineering`
matches via its second segment even though `Software Engineer` alone would
not, and `Database Administrator` never matches (no word boundary between
"data" and "base"). An exclude pattern disqualifies the **whole title**
regardless of which segment it's in — `Senior Manager, Data Science` is
rejected even though "Data Science" sits in a different comma segment from
"Manager"; a per-segment-only exclude would have let it through.

`config/settings.yaml`'s `locations` list is the **whole** DFW filter — a city
missing from it is dropped silently. It covers the metro (Dallas, Tarrant,
Collin, Denton and Rockwall county cities), not just the handful nearest
downtown: the original twelve entries excluded Arlington, Carrollton,
Lewisville, Denton, McKinney, Allen, Garland, Southlake, Flower Mound and Grand
Prairie, all ordinary commutable employer locations.

DFW matching rejects same-named cities in other states, so `Westlake Village, CA`,
`Richardson, UT` and `Arlington, VA` do not pass as DFW. That guard
(`enforce_texas_for_city_match`) is what makes it safe to list the more generic
city names.

A remote role is only `remote_us` when nothing contradicts it. A bare "Remote"
or "Work from home" has no geography to judge and is trusted; a location that
still names a place once the remote wording is stripped needs positive U.S.
evidence — a state, or the country. The blocklist of foreign countries cannot
be exhaustive, and `_has_us_signal` used to end with "…or the text contains a
remote token", which every remote posting's text does, so the check returned
True every time and never ran.

### Careers-site traversal

Most workbook rows give a branded careers page, not an ATS URL, and the real
job list is often several links deep (`Career Areas` → `Jobs` → `Search Jobs`).
The browser fallback explores best-first by link score, bounded three ways so
one sprawling site cannot burn the per-company timeout:

| Setting | Default | Effect |
|---------|---------|--------|
| `hints.enabled` | true | Master switch for the job-list hint cache; false restores pre-hint behaviour exactly |
| `hints.path` | data/browser_hints.json | Where remembered job lists are stored |
| `hints.attempt_seconds` | 20 | Budget for one hint attempt before falling through to discovery |
| `hints.min_yield_ratio` | 0.8 | Rows a hint must return, relative to what the company yielded last run |
| `hints.max_age_days` | 14 | Force a full rediscovery once a hint reaches this age (staggered per company) |
| `hints.max_failures` | 2 | Consecutive non-clean failures before a hint is dropped |
| `playwright.max_hops` | 5 | How many links deep to follow |
| `playwright.max_hop_visits` | 12 | Total pages rendered per company |
| `playwright.hop_budget_seconds` | 100 | Wall-clock ceiling for traversal |
| `playwright.search_at_each_hop` | true | Try each page's own search box, not just the last |
| `playwright.hop_good_enough_rows` | 10 | Rows that count as a real list, not "featured" roles |

The last one matters: landing pages routinely show three featured roles.
Returning those would report 3 jobs for a company with thousands, so a small
result is kept only as a fallback while the search continues. Note that the
floor answers "does this look like a real job list?" and nothing more — a
harvest that clears it can still be page one of many, which is why the cheap
tiers pair it with `detect_more_results()` (see **Collection completeness**).

Pagination inside a rendered page is bounded twice, for the same reason the
job budget and the page ceiling both exist:

| Setting | Default | Effect |
|---------|---------|--------|
| `playwright.max_pages` | 40 | "Load more"/next clicks per company |
| `playwright.pagination_budget_seconds` | 150 | Wall-clock ceiling on that clicking |

A count alone had to stay low (it was 10), because the only other bound was
`browser_company_timeout_seconds`, and **overshooting that is recorded as a
Timeout failure, which discards every row already collected** — strictly worse
than truncation. The wall-clock bound decouples the two: the count can be
generous, and a genuinely slow paginator gives up at the budget and is
reported as truncated instead of failing. Measured against a full run at 10
clicks, IBM stopped at 280 jobs, CBRE at 420 and Goldman Sachs at 220, each
with `[budget_exhausted]` and more still available.

**Raising this budget is not a safe way to buy coverage — measured.** At 150s
six large employers were still stopping with more advertised (Goldman Sachs
820 jobs, IBM 1,016, Jacobs 720, Pyramid 404, Verizon 394, DXC 386), so the
budget was raised to 300s with `browser_company_timeout_seconds` moved 480 →
660 to match. The result was strictly worse. PwC, CBRE and Slalom each ran past
the per-company limit and were abandoned — and **an abandoned company does not
give its worker thread back**. With all three Playwright workers wedged the
phase dropped to zero throughput and the fourteen companies queued behind them
died on the phase budget instead. One run to the next: Goldman Sachs 820 → 0,
IBM 1,016 → 0, Jacobs 720 → 0, Pyramid 404 → 0, Verizon 394 → 0, Kelly 100 → 0,
Randstad 132 → 0; total failures 3 → 20. Every company the raise was meant to
help finished with nothing.

So the budget is back at 150s, and the ordering is: make an abandoned company
release its worker *first*, then this can rise. `browser_company_timeout_seconds`
is 600 rather than the original 480 for the same reason — since abandonment
costs the whole phase and buys almost nothing, the bias should be toward
letting a company finish.

**Scrolling: page height is not the test.** The lazy-load path used to compare
`document.body.scrollHeight` before and after a scroll and call the walk
complete when it did not grow. A *virtualized* list — react-window, ag-grid,
and the enterprise careers grids built on them — keeps its scroll height fixed
by design, recycling a small pool of DOM rows across thousands of records, so
height never grows on exactly the sites where scrolling matters most. The walk
stopped after one screen and reported a **complete** harvest, which lets
removal sync delete every posting it never reached. A height-neutral scroll is
now inconclusive rather than final: the row-level barren counter, which tracks
whether jobs are still arriving, decides instead.

### Two User-Agents, deliberately

`requests.user_agent` is a bare `Mozilla/5.0` because a full Chrome UA trips
AWS WAF's bot captcha on several iCIMS tenants. `playwright.user_agent` is a
full Chrome string because Cloudflare rejects the bare one outright in a real
browser context. Neither value works for both paths — changing either to match
the other silently costs coverage.

---

## Logging

**One file, describing the current run only:** `logs/scraper.log`
(`logging.file` in `config/settings.yaml`).

Each run opens it with `mode="w"`, so starting a run replaces the previous
run's log rather than appending to it. There are no timestamped per-run logs
and no rotated `.1`/`.2`/`.3` siblings — nothing accumulates, and "the log" is
never ambiguous.

That replaced a `RotatingFileHandler` appending behind three 5 MB backups,
which was wrong in two ways. Diagnosing a run meant first locating where it
began inside a file holding several; and on a long run the rotation could
discard that run's *own beginning* to make room for its end — the part you most
want when a company failed early.

| Property | Behaviour |
|----------|-----------|
| Location | `logs/scraper.log`; the directory is created if absent |
| Lifetime | The latest run only — truncated at the start of the next one |
| File level | Always `DEBUG`, regardless of console verbosity |
| Console level | `logging.level` (default `INFO`); `--quiet` silences the console without affecting the file |
| Concurrency | Workers are threads sharing one handler, so writes are serialised by `logging`'s own lock — no interleaved or torn lines |
| Crash safety | Written incrementally, so an interrupted or failed run still leaves its log behind |
| Tests | `tests/conftest.py` redirects `setup_logging` into `tmp_path` for every test — a stray call would otherwise *truncate* the log of the run being diagnosed |

`setup_logging(..., fresh=False)` appends instead, for tools that are not runs
of their own: `tools/canary.py` writes `logs/canary.log`, `find_ats_urls.py`
writes `logs/discovery.log`, `probe_site.py` writes `logs/probe.log`. Each is
its own single current file under the same policy.

The dashboard follows the same policy without going through `setup_logging` at
all: `logs/dashboard_run.log` holds the **console output** of the current
dashboard-launched run and is truncated at each start. It exists for the case
`scraper.log` cannot cover — a run that dies before logging is configured (a
bad config, an import error) still prints why. See [Dashboard](#dashboard).

Run *statistics* are a separate concern and unaffected: `output/last_run.json`
still carries the full per-company record (see [Output](#output)).

---

## Reliability

**One wedged company must not cost the phase.** Playwright threads can block
inside their own event loop with no timeout of their own. The per-company limit
records such a company as failed — but a `ThreadPoolExecutor` has no way to
reclaim the thread, so the slot stays occupied until the process exits.
Measured: PwC, CBRE and Slalom each ran past the limit within ten minutes of
each other, all three Playwright workers were then permanently held, throughput
went to zero, and the fourteen companies queued behind them were written off on
the phase budget. **Three bad sites cost seventeen.**

The obvious fix — spare threads, so an abandoned company's slot can be reused —
was implemented, measured and reverted. Playwright's sync API is thread-affine
(`shutdown_thread_browser` documents why: closing a worker's browser from
another thread deadlocked a full run and orphaned ~100 Chromium processes), so
the wedged browser cannot be closed and the replacement starts a *second*
Chromium beside it. Six concurrent instances against a measured ceiling of five
turned a 43-minute run with 3 failures into a **3h13m run with 19**. The scarce
resource is browsers, not threads, and no amount of thread juggling makes more
of them.

What works costs nothing: `pipeline.slowest_last()` orders each phase so the
companies that timed out on the previous run go **last**, and healthy companies
are ordered fastest-first ahead of them. Whatever a wedged company blocks, it
blocks companies that were already the slowest or already timed out — and if
the phase budget expires, it expires on them rather than on healthy employers
that merely queued behind them. Omnicell and Slalom have timed out on every
recorded run; under this they can no longer take anything with them. It is
deliberately a *deprioritisation*, never a skip list, so a site that recovers
is still scraped.


- 30s HTTP timeout, 3 retries, exponential backoff **with jitter** (tenacity),
  `Retry-After` honoured on 429. Jitter matters because ten workers share one
  retry schedule — without it they back off in lockstep and retry together,
  turning a transient 503 into a sustained one
- **Per-host rate limiting** (`requests.per_host_rate_per_second`, default 3/s),
  shared across all workers and keyed on hostname so a slow vendor never
  throttles unrelated companies. Raising the pagination ceiling multiplied what
  one company can request — a large Workday tenant went from 25 requests to as
  many as 500 — and pacing is what keeps that polite
- **Bounded response reads** (`http_client.MAX_RESPONSE_BYTES`, 8 MB): bodies
  are streamed and truncated rather than read whole, since an unbounded `.text`
  across 10 workers is a memory-exhaustion risk
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
| `main.py` | CLI: arg parsing, `--dry-run`/`--test-*`/`--retry-failed` modes, wiring to `pipeline.run()` |
| `pipeline.py` | Run orchestration — load workbook → route → execute (2 thread pools) → filter/dedupe/store → write outputs, `last_run.json` and workbook write-back. Also `company_status()` (the five outcomes), `write_run_report()`, `retryable_companies()` / `retryable_from_report()`, the retry merge (`removal_sync_allowed()`, `merge_job_rows()`, `merge_run_reports()`, `write_merged_outputs()`, `speaks_for_whole_workbook()`) and `select_attachments()` |
| `settings.py` | Loads `config/settings.yaml`; path resolution and config access |
| `logger.py` | Logging setup |
| `http_client.py` | Shared `requests` session, retries/backoff, `get_json`/`get_text`/`post_json` |
| `dashboard/app.py` | The two-tab Streamlit UI (Run Scraper / Manage Companies). No scraper logic — it renders what `services` returns |
| `dashboard/services.py` | The dashboard's non-UI half: the cross-process run lock, launching `main.py` (a full run or a `--retry-failed` one), reading `last_run.json` / the current export / the log, and the safe `companies.xlsx` writer. Imports no Streamlit |
| `dashboard/runner.py` | Supervisor for one dashboard-launched run: spawns `main.py`, captures its console output, records the real **exit code** and releases the lock |

### Routing & detection (`ats/`)

| File | Responsibility |
|------|----------------|
| `ats/router.py` | The ladder: `plan_route()` (decide provider+method) and `fetch_company_jobs()` (API → JSON-LD → static HTML → framework data → Playwright, with mid-run self-heal). `COLLECTORS` dict = supported providers. `BrowserHarvest` carries the browser fallback's rows *and* its completeness back to `CompanyResult` |
| `ats/detector.py` | Lexical ATS detection from a URL, plus HTML fingerprints and embedded-URL extraction. **Add a new provider's host/fingerprint here** |
| `ats/resolver.py` | One HTTP GET on a branded page → identify the ATS behind it (redirect/fingerprint/embedded URL); 403→browser-UA retry |
| `ats/url_repair.py` | Swaps a dead `careers.*` subdomain for a live careers page before routing |
| `ats/base.py` | `ATSCollector` base class, `CollectionResult` (the completeness contract) + `CollectorUnavailable`; `record()`/`finalize()`/`result()` helpers every collector uses |
| `ats/pagination.py` | The shared pagination walk: per-page retry, repeated-page detection, total reconciliation, the provider's real page size, budget. **All fourteen paginating collectors use this**, Taleo included |
| `ats/html_utils.py` | Shared HTML/JSON-LD parsing helpers for collectors |
| `ats/discovery.py` | On-demand ATS-URL discovery engine (used by `tools/find_ats_urls.py`, **not** the live pipeline) |

### Collectors (`ats/`, one per provider — 18 + generic tier)

| File | Provider |
|------|----------|
| `workday.py` `greenhouse.py` `lever.py` `ashby.py` `smartrecruiters.py` | documented public APIs |
| `paylocity.py` `ukg.py` `taleo.py` `icims.py` `phenom.py` | |
| `successfactors.py` `avature.py` `eightfold.py` `radancy.py` | |
| `amazon.py` `jobvite.py` `cornerstone.py` `jibe.py` | added in the coverage-expansion branch |
| `jsonld.py` `static_html.py` `framework_data.py` | **generic** tiers — provider-agnostic, one GET each, tried before the browser |

To add a provider: register its host/fingerprint in `detector.py`, write
`ats/<provider>.py` subclassing `ATSCollector`, add it to `COLLECTORS` in
`router.py`, and add an offline test. Ship it only once a real workbook company
returns real jobs through it.

### Browser fallback (`browser/`)

| File | Responsibility |
|------|----------------|
| `browser/playwright_scraper.py` | Keyword-search + best-first hop traversal, JSON-LD extraction, cookie dismissal, network sniffing for ATS discovery, stealth, retry with rotated fingerprint. `scrape_entry_url()` is the hint fast path: render a remembered job list and paginate it, with no hopping or searching |
| `browser_hints.py` | Per-company memory of *where* the browser found a job list (`entry_url`) and *what served it* (`json_endpoint`), plus the rules deciding when a hint is trusted, kept or thrown away |

### Post-scrape tail (the part you said won't change)

| File | Responsibility |
|------|----------------|
| `normalize.py` | Build the canonical job record; clean text, parse dates, join locations |
| `filters.py` | Role match (per title segment), DFW/remote match, freshness window |
| `enrich.py` | Fill coarse locations (e.g. Workday detail fetch) |
| `deduplicate.py` | Collapse duplicate postings within a run |
| `fit.py` | Explainable fit scoring against a configurable skill list |
| `notify.py` | Email digest of new/changed jobs; the `EMAIL_ENABLED` gate, recipient/sender/TLS resolution, dry-run rendering. Every value from env only |
| `job_identity.py` | Stable, company-scoped per-job id derived from the posting URL; `JOB_ID_SCHEME_VERSION` |
| `database.py` | SQLite tracking — upsert, per-company removal sync, new/first-seen |
| `export_ats_urls.py` | Write verified discovered ATS URLs, verified dead-URL repairs, and run status back into the workbook |

### Config, tools, tests, docs

| Path | Responsibility |
|------|----------------|
| `config/settings.yaml` | All tunables (freshness, roles, DFW cities, HTTP, Playwright, concurrency, logging) |
| `config/companies.xlsx` | Input workbook (Company / ATS URL / Live Jobs Page) — 180 companies, all names unique |
| `.env.example` | Tracked template for the email variables; carries no values. Copy to `.env` (gitignored) |
| `tools/canary.py` | ~2-min smoke test: one company per collection path (run before a full run) |
| `tools/find_ats_urls.py` | Crawl + verify missing ATS URLs, write suggestions into the workbook |
| `tools/probe_site.py` | Diagnostic: dump what a single page actually contains |
| `requirements-dashboard.txt` | Streamlit, for `streamlit run dashboard/app.py`. Separate from `requirements.txt` so a scraper deployment does not install a web server |
| `.streamlit/config.toml` | Hides Streamlit's Deploy button (and with it Rerun / Clear cache); disables usage stats. See [Dashboard](#dashboard) |
| `dashboard/start_dashboard.bat` / `stop_dashboard.bat` / `create_desktop_shortcut.bat` | Start the server with no console window, stop it, and put a one-click shortcut on the Desktop |
| `tests/` | Offline pytest suite (network mocked), 1,025 tests |
| `tests/conftest.py` | Suite-wide isolation: clears the `.env` variables, and redirects `setup_logging` into `tmp_path` so no test can truncate `logs/scraper.log` |
| `docs/superpowers/` | Design specs + implementation plans — see `docs/superpowers/README.md` for the index |

## Before trusting a full run

A full run over all 180 companies takes **~40 minutes** (measured: 145,634 jobs
collected, 122 direct-API and 58 browser companies, 3 Playwright workers, 178
of 180 succeeding).
`tools/canary.py` checks one company per collection path in about two minutes
and exits non-zero if any path returns zero jobs — it catches the case where a
whole provider (or the browser path) breaks silently:

```bash
python tools/canary.py
```

Unit tests: `python -m pytest tests/ -q` (1,025 tests, ~10 minutes).

Lint: `python -m ruff check --select F,E9 --exclude venv .` — `F` catches
unused imports and dead locals, `E9` catches syntax/IO errors. Kept to those
two families deliberately: this codebase carries long explanatory comments and
deliberately wide lines, and a full default rule set would report hundreds of
style opinions that say nothing about whether jobs are being missed.

After a run, `output/last_run.json` is the fastest way to see what happened:
the `status_counts` block, and `removal_sync_allowed` per company. Re-run just
the ones that did not finish with `python main.py --retry-failed` — the results
merge back into that same file and the same export, per company.

Test-only dependencies live in `requirements-dev.txt` (which includes
`requirements.txt`), so a deployment does not install a test runner it will
never use:

```bash
pip install -r requirements-dev.txt
```

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
| Anything under `dashboard/` — a field shown, a guard, a file it owns | [Dashboard](#dashboard) + [Entry points & tools](#entry-points--tools) |
| Any module's purpose, or add/remove a file | [Codebase map](#codebase-map) |
| A setting in `config/settings.yaml` | [Configuration](#configuration) |
| Output columns, `last_run.json`, or filtering behaviour | [Output](#output) |
| Email configuration or an env variable | [Notifications](#notifications) table + `.env.example` |
| A design decision worth recording | add/refresh a doc under [`docs/superpowers/`](docs/superpowers/README.md) |

If a change makes a section wrong, fixing the doc is part of finishing the
change — not a follow-up.
