"""Local dashboard for the company ATS scraper.

    streamlit run dashboard/app.py        # then open http://localhost:8501

Two tabs and nothing else: **Run Scraper** starts the same ``python main.py``
a terminal would and reports what the run's own files say happened, and
**Manage Companies** adds or edits a row in ``config/companies.xlsx``.

There is no scraper logic here. Every number on the page comes from
``output/last_run.json``, ``output/company_jobs.csv`` or the run lock; every
action is a call into :mod:`dashboard.services`. Nothing is rendered with
``unsafe_allow_html``, so scraped job titles and workbook text are escaped by
Streamlit rather than trusted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Running under `streamlit run dashboard/app.py` puts dashboard/ on sys.path,
# not the project root, so the scraper's own modules would not import.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dashboard import services  # noqa: E402
from settings import load_settings  # noqa: E402

REFRESH_SECONDS = 3
LOG_TAIL_LINES = 120
#: Pixel height of the log boxes. Fixed rather than "as tall as the content",
#: which let a 120-line tail push everything below it off the screen; a fixed
#: height makes the block scroll internally instead.
LOG_BOX_HEIGHT = 300
ERROR_BOX_HEIGHT = 180

STATUS_BADGE = {
    services.STATUS_IDLE: ("Idle", "off"),
    services.STATUS_RUNNING: ("Running", "running"),
    services.STATUS_COMPLETED: ("Completed", "complete"),
    services.STATUS_PARTIAL: ("Partial", "complete"),
    services.STATUS_FAILED: ("Failed", "error"),
}


# ---------------------------------------------------------------------------
# Cached reads. Keyed on the file's mtime so a finished run invalidates them
# without anyone having to remember to clear a cache.
# ---------------------------------------------------------------------------

def _stamp(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def _jobs_frame(_settings_path: str, stamp: float) -> pd.DataFrame:
    return services.load_current_jobs()


@st.cache_data(show_spinner=False)
def _workbook_frame(_settings_path: str, stamp: float) -> pd.DataFrame:
    return services.read_workbook()


@st.cache_data(show_spinner=False)
def _file_bytes(path_text: str, stamp: float) -> bytes:
    return Path(path_text).read_bytes()


def _fact(column, label: str, value: str, sub: str = "") -> None:
    """A compact label/value pair: small grey caption, body-size value.

    The alternative, ``st.metric``, is right for the short integer counts below
    and wrong for a timestamp - its value type is about twice body size, which
    a date plus a time cannot fit into one column without wrapping.
    """
    with column:
        st.caption(label)
        st.markdown(f"**{value}**")
        if sub:
            st.caption(sub)


def _refresh_button(key: str, label: str = "Refresh", help_text: str = "") -> None:
    """Re-read every file this page draws from, and redraw.

    This is the page's own replacement for Streamlit's built-in "Rerun", which
    is hidden along with the Deploy button by ``.streamlit/config.toml`` (1.62
    has no switch for Deploy alone). Two reasons it is an improvement rather
    than a like-for-like: "Rerun" describes a mechanism and left people
    guessing whether it started a scrape, and it did not clear the cached file
    reads - which is only invisible because they are keyed on modification
    time.

    It reads files. It never starts, stops or affects a run.
    """
    if st.button(
        label, key=key, width="stretch",
        help=help_text or "Re-reads the workbook, the run report, the export and the log. "
                          "Does not start a scraper run.",
    ):
        st.cache_data.clear()
        st.rerun()


def _flash(kind: str, message: str) -> None:
    """Queue a message to show after the rerun that follows an action."""
    st.session_state.setdefault("flash", []).append((kind, message))


def _render_flashes() -> None:
    for kind, message in st.session_state.pop("flash", []):
        getattr(st, kind, st.info)(message)


# ---------------------------------------------------------------------------
# Tab 1 - Run Scraper
# ---------------------------------------------------------------------------

def _run_controls(settings, status: services.RunStatus) -> None:
    left, right = st.columns([2, 3])

    with left:
        dry_run = st.checkbox(
            "Dry run (routing only, scrapes nothing)",
            value=False,
            disabled=status.running,
            help="Runs `python main.py --dry-run`: reads the workbook and prints routing "
                 "decisions without scraping. Outputs are left untouched.",
        )
        if status.stale_lock:
            st.warning(
                "A previous run left a lock behind and its process is gone. "
                "Clear it to run again."
            )
            if st.button("Clear stale run lock", type="primary", width="stretch"):
                try:
                    services.clear_stale_lock(settings)
                except services.DashboardError as exc:
                    _flash("error", str(exc))
                else:
                    _flash("success", "Stale lock cleared. You can start a run now.")
                st.rerun()
        else:
            clicked = st.button(
                "Run Scraper",
                type="primary",
                width="stretch",
                disabled=status.running,
                help="Starts `python main.py --no-email` in a separate process.",
            )
            if clicked:
                try:
                    payload = services.start_run(settings, dry_run=dry_run)
                except services.DashboardError as exc:
                    _flash("error", str(exc))
                else:
                    _flash(
                        "success",
                        f"Run started (PID {payload.get('pid')}): "
                        f"python main.py {' '.join(payload.get('args', []))}",
                    )
                st.rerun()

        if status.running:
            st.caption(
                "A run is already in progress, so the button is disabled - only one run "
                "may be active at a time, across every browser tab and every dashboard "
                "process on this machine."
            )
        else:
            st.caption("The dashboard always passes `--no-email`; no digest is ever sent.")

    with right:
        # vertical_alignment rather than a spacer element: st.markdown escapes
        # raw HTML here (deliberately - see the module docstring), so a &nbsp;
        # shim would render as literal text.
        state, action = st.columns([3, 1], vertical_alignment="bottom")
        with state:
            label, _ = STATUS_BADGE.get(status.status, (status.status.title(), "off"))
            st.metric("Current run status", label)
        with action:
            _refresh_button("refresh_run_tab")
        if status.detail:
            st.caption(status.detail)
        if status.running:
            st.caption(
                f"Elapsed: {services.humanize_duration(status.duration_seconds)}"
                f" - started {services.format_clock(status.started_at)}"
            )


def _panel(title: str, note: str = ""):
    """A bordered group with a heading. Returns its four columns.

    Every panel is four columns wide, always. The figures used to sit in rows
    of five, five, three and four, so nothing lined up down the page and the
    numbers read as scattered rather than as three related groups. A fixed
    width means every value shares a column edge with the one above it, and the
    border does the grouping the uneven widths were trying to do.
    """
    box = st.container(border=True)
    with box:
        st.markdown(f"**{title}**" + (f" - {note}" if note else ""))
        return st.columns(4)


def _run_facts(settings, status: services.RunStatus, jobs: pd.DataFrame) -> None:
    outputs = services.output_files(settings)
    csv_modified, csv_age = services.file_age(outputs["csv"])
    totals = status.totals or {}
    changes = services.change_status_counts(jobs)
    hours = int(settings.get("hours_old", 168) or 168)

    # --- Timing ------------------------------------------------------------
    # Deliberately not st.metric here. A metric renders its value at roughly
    # twice body size, and a full date-and-time does not fit one column at that
    # size - it wrapped into a run-together block of digits.
    timing = _panel("Timing")
    _fact(timing[0], "Last run started",
          services.format_clock(status.started_at),
          services.format_utc(status.started_at))
    _fact(timing[1], "Last run ended",
          services.format_clock(status.finished_at),
          services.format_utc(status.finished_at))
    # last_run.json is written only once a run reaches the end of the pipeline,
    # so its timestamp is the last *successful completion* - which is not the
    # same as the last run ending, and the two differ exactly when it matters.
    _fact(timing[2], "Last successful completion",
          services.format_clock(status.report_generated_at),
          services.format_utc(status.report_generated_at))
    _fact(timing[3], "Run duration",
          services.humanize_duration(status.duration_seconds) or "-",
          "wall clock, start to finish")
    st.caption(
        "Times are shown in this machine's timezone with UTC underneath; 'last successful "
        "completion' is when the pipeline finished and wrote `output/last_run.json`."
    )

    # --- Companies ---------------------------------------------------------
    attempted = status.companies_attempted
    note = f"{attempted:,} attempted" if attempted is not None else "no run recorded"
    if status.no_jobs_companies:
        note += f", {status.no_jobs_companies:,} read correctly but not hiring"
    companies = _panel("Companies", note)
    companies[0].metric("Successful", f"{status.successful_companies:,}")
    companies[1].metric("Partial", f"{status.partial_companies:,}")
    companies[2].metric("Failed", f"{status.failed_companies:,}")
    companies[3].metric("Blocked", f"{status.blocked_companies:,}")
    st.caption(
        "Every partial, failed and blocked company is listed with its reason at the bottom "
        "of this tab."
    )

    # --- Jobs --------------------------------------------------------------
    jobs_panel = _panel("Jobs", "from the current export and the run report")
    jobs_panel[0].metric("Total collected", f"{int(totals.get('jobs_collected', 0) or 0):,}")
    jobs_panel[1].metric("Matching", f"{int(totals.get('matching_jobs', 0) or 0):,}")
    jobs_panel[2].metric(f"Within last {hours}h ({hours // 24}d)",
                         f"{services.within_window_count(jobs):,}")
    jobs_panel[3].metric("Output freshness", csv_age or "-")
    st.caption(
        "'Total collected' is every job the run fetched; 'matching' is what survived the "
        "role, location and freshness filters and reached the export. Output freshness is "
        + (f"the age of `{outputs['csv'].name}`, written "
           f"{services.format_clock(csv_modified, seconds=False)}."
           if csv_modified else "unavailable - no export on disk yet.")
    )

    # --- Change since last run --------------------------------------------
    change = _panel("Change since the previous run")
    change[0].metric("New", f"{changes.get('new', int(totals.get('new_jobs', 0) or 0)):,}")
    change[1].metric(
        "Changed", f"{changes.get('changed', int(totals.get('changed_jobs', 0) or 0)):,}"
    )
    change[2].metric("Unchanged", f"{changes.get('unchanged', 0):,}")
    change[3].metric("Removed", f"{int(totals.get('removed_jobs', 0) or 0):,}")
    st.caption(
        "New / changed / unchanged are counted from `change_status` in the current export; "
        "removed comes from the run report - removed jobs are deliberately not exported, "
        "because a row for a closed requisition is a link to a dead page."
    )

    # --- Identity ----------------------------------------------------------
    identity = st.columns(4)
    _fact(identity[0], "Run ID", status.run_id or "-",
          "the last full run; each exported row carries the run that produced it")
    _fact(identity[1], "Final output", outputs["csv"].name, str(outputs["csv"].parent))
    _fact(identity[2], "Workbook", services.workbook_path(settings).name,
          str(services.workbook_path(settings).parent))
    _fact(identity[3], "Current log", services.log_path(settings).name,
          str(services.log_path(settings).parent))

    if status.report_malformed:
        st.warning(
            f"`{services.last_run_report_path(settings)}` exists but could not be parsed, so "
            "the per-company figures above are unavailable. The run's own log is unaffected."
        )
    elif not status.report_available:
        st.info(
            "No run report yet. `output/last_run.json` is written at the end of a full run; "
            "the figures above fill in once one completes."
        )

    if status.status == services.STATUS_FAILED:
        code = status.exit_code
        st.error(
            f"The last run failed{f' with exit code {code}' if code is not None else ''}."
            + (f" {status.error_message}" if status.error_message else "")
        )
        tail = services.read_log_tail(services.run_log_path(settings), 40)
        if tail:
            st.caption("Last lines of the run's console output:")
            st.code(tail, language="text", height=ERROR_BOX_HEIGHT, wrap_lines=True)


def _live_panel(settings, status: services.RunStatus) -> None:
    if status.running:
        # Progress comes from the log the run already writes - ats.router logs
        # one line per company, and the pipeline logs the total up front.
        progress = services.run_progress(settings)
        if progress.current_company:
            # The router's detail is usually a provider name but can be a full
            # escalation sentence, so it is clipped rather than allowed to wrap
            # the headline over three lines.
            detail = progress.provider
            if len(detail) > 60:
                detail = detail[:57] + "..."
            st.markdown(
                f"**Currently processing** - {progress.current_company}"
                + (f" (`{detail}`)" if detail else "")
            )
        if progress.fraction is not None:
            st.progress(
                progress.fraction,
                text=f"{progress.finished:,} of {progress.total:,} companies finished "
                     f"({progress.started:,} started) - elapsed "
                     f"{services.humanize_duration(status.duration_seconds)}",
            )
        else:
            st.caption(
                "Routing companies - per-company progress appears once the run reaches "
                f"the collection phase. Elapsed "
                f"{services.humanize_duration(status.duration_seconds)}."
            )

    log = services.log_path(settings)
    tail = services.read_log_tail(log, LOG_TAIL_LINES)
    st.caption(
        f"`{log}` - the single current log, truncated at the start of every run "
        f"(last {LOG_TAIL_LINES} lines). Scroll inside the box."
    )
    # A fixed height is what makes the block scroll instead of pushing the job
    # table hundreds of lines down the page; wrap_lines keeps a long Playwright
    # error inside the box rather than scrolling the page sideways.
    st.code(
        tail or "(the log is empty)",
        language="text",
        height=LOG_BOX_HEIGHT,
        wrap_lines=True,
    )


def _jobs_table(settings, jobs: pd.DataFrame) -> None:
    st.subheader("Current job output")
    outputs = services.output_files(settings)

    if jobs.empty:
        st.info(
            f"No job export to show yet. A completed run writes `{outputs['csv']}`; "
            "a dry run does not."
        )
        return

    modified, age = services.file_age(outputs["csv"])
    st.caption(
        f"{len(jobs):,} rows from `{outputs['csv'].name}` - written "
        f"{services.format_clock(modified)} ({age})."
    )

    filters = st.columns([2, 2, 2, 2])
    companies = sorted(jobs["company"].dropna().unique()) if "company" in jobs else []
    chosen = filters[0].multiselect("Company", companies, default=[])
    title = filters[1].text_input("Job title contains", "")
    location = filters[2].text_input("Location contains", "")
    statuses = sorted(
        {status for status in jobs.get("change_status", pd.Series(dtype=str)) if status}
    )
    chosen_status = filters[3].multiselect("Job status", statuses, default=[])

    posted = pd.to_datetime(
        jobs.get("date_posted", pd.Series(dtype=str)),
        errors="coerce", utc=True, format="mixed",
    )
    date_range = ()
    if posted.notna().any():
        earliest, latest = posted.min().date(), posted.max().date()
        date_range = st.date_input(
            "Posted date",
            value=(earliest, latest),
            min_value=earliest,
            max_value=latest,
            help="Rows with no reliable posting date are always kept - the pipeline flags "
                 "them rather than discarding them.",
        )
        if not isinstance(date_range, tuple):
            date_range = (date_range,)

    filtered = services.filter_jobs(
        jobs,
        companies=chosen,
        title=title,
        location=location,
        statuses=chosen_status,
        posted_from=date_range[0] if len(date_range) > 0 else None,
        posted_to=date_range[1] if len(date_range) > 1 else None,
    )

    display_columns = [
        column for column in (
            "company", "title", "location", "date_posted", "job_url", "apply_url",
            "ats_provider", "scraping_method", "change_status", "date_filter_status",
            "date_source", "remote_scope",
        ) if column in filtered.columns
    ]
    st.caption(f"Showing {len(filtered):,} of {len(jobs):,} rows.")
    st.dataframe(
        filtered[display_columns],
        width="stretch",
        hide_index=True,
        column_config={
            "company": st.column_config.TextColumn("Company"),
            "title": st.column_config.TextColumn("Job title", width="large"),
            "location": st.column_config.TextColumn("Location"),
            "date_posted": st.column_config.TextColumn("Posted date"),
            "job_url": st.column_config.LinkColumn("Application URL", display_text="Open"),
            "apply_url": st.column_config.LinkColumn("Apply URL", display_text="Apply"),
            "ats_provider": st.column_config.TextColumn("Provider"),
            "scraping_method": st.column_config.TextColumn("Extraction method"),
            "change_status": st.column_config.TextColumn("Job status"),
            "date_filter_status": st.column_config.TextColumn("Freshness"),
            "date_source": st.column_config.TextColumn("Date source"),
            "remote_scope": st.column_config.TextColumn("Remote scope"),
        },
    )

    st.markdown("**Downloads** - the files the run itself wrote, unmodified.")
    buttons = st.columns(3)
    for column, key, label, mime in (
        (buttons[0], "csv", "company_jobs.csv", "text/csv"),
        (buttons[1], "xlsx", "company_jobs.xlsx",
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (buttons[2], "failures", "scraper_failures.csv", "text/csv"),
    ):
        path = outputs[key]
        if path.exists():
            column.download_button(
                f"Download {path.name}",
                data=_file_bytes(str(path), _stamp(path)),
                file_name=path.name,
                mime=mime,
                width="stretch",
            )
        else:
            column.button(f"{label} not written yet", disabled=True, width="stretch")


def _problem_table(settings, status: services.RunStatus, report: dict | None) -> None:
    st.subheader("Companies needing attention")
    frame = services.problem_companies(report)
    if frame.empty:
        st.success("No partial, failed or blocked companies in the last run report.")
        return
    st.caption(
        "`partial` means the rows are real but the coverage is not (that is what the "
        "re-run button below covers); `blocked` means the site issued a challenge "
        "and needs a different route in, never a retry."
    )
    st.dataframe(frame, width="stretch", hide_index=True)
    _retry_control(settings, status, report, len(frame))


def _retry_control(
    settings, status: services.RunStatus, report: dict | None, problem_count: int
) -> None:
    """Re-run only the companies above that are worth a second attempt.

    The list is the pipeline's own (``pipeline.retryable_from_report``), read
    from the same ``last_run.json`` the table above is drawn from, so the
    button can never disagree with what the run will actually do - it names
    the count and shows the names before anything is launched.
    """
    targets = services.retry_targets(report)

    # The report is merged in place, so without this the page would show a
    # retry's results with nothing saying a retry had happened.
    retry = services.last_retry(report)
    if retry:
        st.caption(
            f"Last retry: {len(retry.companies)} company(ies) at "
            f"{services.format_clock(retry.finished_at)} (run `{retry.run_id}`), "
            "merged into the figures above."
        )

    left, right = st.columns([2, 3])

    with left:
        if not targets:
            st.button(
                "Re-run companies needing attention",
                width="stretch",
                disabled=True,
                help="Every company above is `blocked`, which a retry cannot fix.",
            )
        elif st.button(
            f"Re-run {len(targets)} companies needing attention",
            width="stretch",
            disabled=status.running,
            help="Starts `python main.py --no-email --retry-failed`: only the partial "
                 "and failed companies from the last run, in a separate process.",
        ):
            try:
                payload = services.start_run(settings, extra_args=["--retry-failed"])
            except services.DashboardError as exc:
                _flash("error", str(exc))
            else:
                _flash(
                    "success",
                    f"Retry started (PID {payload.get('pid')}) for {len(targets)} "
                    f"company(ies): python main.py {' '.join(payload.get('args', []))}",
                )
            st.rerun()

    with right:
        if targets:
            skipped = problem_count - len(targets)
            st.caption(
                f"Re-runs the {len(targets)} `partial`/`failed` companies only."
                + (
                    f" The {skipped} `blocked` one(s) are skipped - a challenge page "
                    "answers a retry the same way."
                    if skipped
                    else ""
                )
            )
            st.caption(
                "Its rows are merged into the same `company_jobs.*` and the same run "
                "report this page already shows - per company, so nothing it did not "
                "visit is touched. There is no second file to check."
            )
        if status.running:
            st.caption("Disabled while a run is in flight - only one run may be active.")

    if targets:
        with st.expander(f"Which {len(targets)} companies", expanded=False):
            for name in targets:
                st.markdown(f"- {name}")


def render_run_tab(settings) -> None:
    status = services.run_status(settings)
    report = services.load_last_run(settings)
    jobs = _jobs_frame(str(settings.path), _stamp(services.output_files(settings)["csv"]))

    _run_controls(settings, status)
    st.divider()
    _run_facts(settings, status, jobs)
    st.divider()

    # Only the log panel re-runs on a timer, and only while a run is in flight.
    # A fragment is what keeps the page responsive: the rest of the script is
    # not re-executed every three seconds, and a click is never queued behind a
    # sleep. It writes at its own call site rather than into a container made
    # outside it, because a fragment only replaces the elements it owns.
    fragment = st.fragment(run_every=REFRESH_SECONDS if status.running else None)

    @fragment
    def _live() -> None:
        current = services.run_status(settings)
        st.subheader("Run log")
        _live_panel(settings, current)
        if status.running and not current.running:
            # The run just ended: pull the *whole page* back (scope="app", not
            # the fragment's default) so the metrics, the job table and the
            # download buttons all reflect it, not just the log.
            st.rerun(scope="app")

    _live()

    st.divider()
    _jobs_table(settings, jobs)
    st.divider()
    _problem_table(settings, status, report)


# ---------------------------------------------------------------------------
# Tab 2 - Manage Companies
# ---------------------------------------------------------------------------

def render_companies_tab(settings) -> None:
    path = services.workbook_path(settings)
    columns = services.workbook_columns(settings)
    running = services.is_run_active(settings)

    st.caption(f"Active workbook: `{path}`")

    try:
        frame = _workbook_frame(str(settings.path), _stamp(path))
    except services.DashboardError as exc:
        st.error(str(exc))
        return

    if running:
        st.warning(
            "A scraper run is in progress, so the workbook is read-only. The run reads it "
            "and writes discovered ATS URLs back into it; editing it now would race that."
        )
    elif services.workbook_is_open_in_excel(path):
        st.warning(
            f"`{path.name}` looks locked by Excel: `{services.excel_lock_file(path).name}` is "
            "present beside it, or the file itself is write-locked. Adding and editing are "
            "refused rather than risking a corrupt workbook. Close it in Excel - and if Excel "
            f"is already closed, the `{services.excel_lock_file(path).name}` file was left "
            "behind by a crash and can be deleted."
        )

    heading, action = st.columns([4, 1], vertical_alignment="bottom")
    heading.subheader(f"Companies ({len(frame):,} rows)")
    with action:
        _refresh_button(
            "refresh_companies_tab", "Refresh",
            help_text="Re-reads config/companies.xlsx. Use this after editing the workbook "
                      "in Excel so the table below shows your change.",
        )
    st.caption(
        "Every column of the workbook, exactly as stored. `Data Retrieved` and `Jobs Found` "
        "are written by the scraper; the `Suggested…` columns by `tools/find_ats_urls.py`."
    )
    st.dataframe(frame, width="stretch", hide_index=True)

    st.divider()
    st.subheader("Add a company")
    with st.form("add_company", clear_on_submit=False):
        name = st.text_input(f"{columns['company']} *", "", max_chars=200)
        ats_url = st.text_input(columns["ats_url"], "", help="Leave blank if unknown - the "
                                "pipeline discovers and back-fills it during a run.")
        live_url = st.text_input(columns["live_jobs_url"], "")
        allow_duplicate = st.checkbox(
            "Add anyway if the URL already belongs to another company", value=False
        )
        submitted = st.form_submit_button("Add company", type="primary", disabled=running)

    if submitted:
        try:
            result = services.add_company(
                name, ats_url, live_url,
                settings=settings, allow_duplicate_url=allow_duplicate,
            )
        except services.DashboardError as exc:
            _flash("error", str(exc))
        else:
            for warning in result.warnings:
                _flash("warning", warning)
            _flash(
                "success",
                f"Added {result.company!r} to {path.name} (row {result.row}). It will be "
                f"included in the next scraper run. One recoverable backup is kept at "
                f"{result.backup.name}.",
            )
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Edit a company")
    st.caption(
        "URLs only. The company name is the key the run report, the SQLite job ids and the "
        "workbook write-back all match on, so renaming a row here would orphan its history. "
        "The workbook has no active/inactive column and the scraper reads every row, so the "
        "dashboard does not offer a deactivation toggle it could not honour - remove a "
        "company by deleting its row in Excel."
    )
    try:
        editable = services.companies_for_editing(settings)
    except services.DashboardError as exc:
        st.error(str(exc))
        return
    if not editable:
        st.info("The workbook has no companies to edit yet.")
        return

    names = [row["name"] for row in editable]
    selected = st.selectbox("Company", names, index=0, key="edit_company")
    current = next(row for row in editable if row["name"] == selected)

    with st.form("edit_company_form"):
        new_ats = st.text_input(columns["ats_url"], current["ats_url"], key="edit_ats")
        new_live = st.text_input(
            columns["live_jobs_url"], current["live_jobs_url"], key="edit_live"
        )
        allow_duplicate_edit = st.checkbox(
            "Save anyway if the URL already belongs to another company",
            value=False, key="edit_dupe",
        )
        saved = st.form_submit_button("Save changes", disabled=running)

    if saved:
        try:
            result = services.update_company(
                selected, new_ats, new_live,
                settings=settings, allow_duplicate_url=allow_duplicate_edit,
            )
        except services.DashboardError as exc:
            _flash("error", str(exc))
        else:
            for warning in result.warnings:
                _flash("warning", warning)
            _flash(
                "success",
                f"Updated {result.company!r} (row {result.row}). The change takes effect on "
                "the next scraper run.",
            )
        st.cache_data.clear()
        st.rerun()


# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Company ATS Job Scraper", page_icon="*", layout="wide")
    st.title("Company ATS Job Scraper")

    try:
        settings = load_settings()
    except (FileNotFoundError, ValueError) as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()
        return

    _render_flashes()
    run_tab, companies_tab = st.tabs(["Run Scraper", "Manage Companies"])
    with run_tab:
        render_run_tab(settings)
    with companies_tab:
        render_companies_tab(settings)


if __name__ == "__main__":
    main()
