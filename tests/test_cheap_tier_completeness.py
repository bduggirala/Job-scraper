"""A single-GET tier must not report page one as a company's whole job list.

The regression these cover was measured live. UT Southwestern Medical Center's
careers page shows its ten newest openings beside a "View all New Jobs" link
and carries no pagination markup at all; the static-HTML tier extracted exactly
ten rows, cleared the ``hop_good_enough_rows`` floor of ten, and returned them
as a complete harvest. ``sync_completed_companies`` then aged out everything
else, leaving that employer with exactly ten stored jobs. Energy Transfer went
the same way, and Randstad USA's page states 5,358 results next to the 132 rows
the tier could read.

Two separate guarantees are asserted here, because either one alone still loses
jobs:

* the tiers detect the evidence and mark the harvest incomplete, so removal
  sync skips the company (:mod:`ats.html_utils`, the collectors); and
* the router refuses to end the ladder on an incomplete harvest, so the
  browser still gets its chance to paginate (:mod:`ats.router`).
"""

from __future__ import annotations


from ats import router
from ats.base import STOP_MORE_AVAILABLE, CollectionResult
from ats.html_utils import detect_more_results
from ats.router import RoutePlan
from ats.static_html import StaticHTMLCollector


# --- the evidence detector -------------------------------------------------

_JOB_ROWS = "".join(
    f'<a href="/jobs/engineer-{i}">Engineer {i}</a>' for i in range(10)
)


def test_a_page_with_no_pagination_evidence_is_treated_as_complete():
    html = f"<html><body><ul>{_JOB_ROWS}</ul></body></html>"
    total, reason = detect_more_results(html, 10)
    assert reason is None
    assert total is None


def test_a_stated_result_count_above_what_we_read_is_evidence():
    html = f"<html><body><p>5,358 jobs found</p>{_JOB_ROWS}</body></html>"
    total, reason = detect_more_results(html, 10)
    assert total == 5358
    assert reason and "5358" in reason


def test_a_stated_count_we_actually_reached_is_not_evidence():
    """Reading every row a page claims is the successful case, not a shortfall."""
    html = f"<html><body><p>10 jobs found</p>{_JOB_ROWS}</body></html>"
    total, reason = detect_more_results(html, 10)
    assert total == 10
    assert reason is None


def test_showing_x_of_y_is_evidence():
    html = f"<html><body><p>Showing 1 - 10 of 1,234</p>{_JOB_ROWS}</body></html>"
    total, reason = detect_more_results(html, 10)
    assert total == 1234
    assert reason is not None


def test_a_rel_next_link_is_evidence():
    html = f'<html><head><link rel="next" href="/jobs?page=2"></head><body>{_JOB_ROWS}</body></html>'
    _, reason = detect_more_results(html, 10)
    assert reason and "next" in reason


def test_a_pagination_widget_is_evidence():
    """Apex Systems' page carries exactly this and no stated count."""
    html = f'<html><body>{_JOB_ROWS}<nav class="pagination"><a href="/2">2</a></nav></body></html>'
    _, reason = detect_more_results(html, 25)
    assert reason and "pagination" in reason


def test_a_view_all_jobs_link_is_evidence():
    """UT Southwestern: a teaser list beside a link to the real one.

    No stated count, no ``rel="next"``, no pagination widget - the only signal
    the page gives is that it points somewhere fuller.
    """
    html = f'<html><body>{_JOB_ROWS}<a href="/latest-jobs">View all New Jobs</a></body></html>'
    _, reason = detect_more_results(html, 10)
    assert reason and "View all New Jobs" in reason


def test_a_next_page_link_is_evidence():
    html = f'<html><body>{_JOB_ROWS}<a href="/search?page=2">Next</a></body></html>'
    _, reason = detect_more_results(html, 10)
    assert reason is not None


def test_a_view_all_link_beside_a_large_list_is_not_evidence():
    """Aveanna Healthcare: 3,708 real rows beside "View All Jobs Near Me".

    A "view all" link is weak evidence - ordinary navigation carries one too -
    so it counts only while the harvest is small enough to actually be a
    teaser. Without this gate the fix throws away a complete 3,708-row harvest
    and sends the company to a browser that returns far less.
    """
    many = "".join(
        f'<a href="/jobs/nurse-{i}">Nurse {i}</a>' for i in range(3708)
    )
    html = f'<html><body>{many}<a href="/near-me">View All Jobs Near Me</a></body></html>'
    _, reason = detect_more_results(html, 3708)
    assert reason is None


def test_a_strong_signal_still_counts_against_a_large_list():
    """Only the "view all" signal is size-gated; a stated total is not.

    Randstad USA: 132 readable rows on a page that says 5,358.
    """
    many = "".join(f'<a href="/jobs/{i}">Engineer {i}</a>' for i in range(132))
    html = f"<html><body><p>5,358 jobs</p>{many}</body></html>"
    total, reason = detect_more_results(html, 132)
    assert total == 5358
    assert reason is not None


def test_a_marketing_headline_is_not_a_result_count():
    """"Over 2,000,000 jobs posted" is a claim about the vendor, not this list."""
    html = f"<html><body><h1>2,000,000 jobs posted</h1>{_JOB_ROWS}</body></html>"
    total, reason = detect_more_results(html, 10)
    assert total is None
    assert reason is None


# --- the static-HTML collector ---------------------------------------------

def _static_collector(monkeypatch, html: str) -> StaticHTMLCollector:
    import http_client

    monkeypatch.setattr(http_client, "get_text", lambda *a, **k: html)
    return StaticHTMLCollector("Acme", {"url": "https://acme.example/careers"})


def test_static_html_marks_a_teaser_page_incomplete(monkeypatch):
    html = f'<html><body>{_JOB_ROWS}<a href="/all">View all jobs</a></body></html>'
    result = _static_collector(monkeypatch, html).collect()

    assert len(result.jobs) == 10
    assert result.complete is False
    assert result.stop_reason == STOP_MORE_AVAILABLE


def test_static_html_still_reports_a_plain_list_as_complete(monkeypatch):
    """The fix must not suppress removal sync for every static-HTML company."""
    html = f"<html><body><ul>{_JOB_ROWS}</ul></body></html>"
    result = _static_collector(monkeypatch, html).collect()

    assert len(result.jobs) == 10
    assert result.complete is True
    assert result.stop_reason is None


def test_static_html_carries_the_pages_own_total(monkeypatch):
    html = f"<html><body><p>5,358 jobs found</p>{_JOB_ROWS}</body></html>"
    result = _static_collector(monkeypatch, html).collect()
    assert result.reported_total == 5358


# --- the router ladder -----------------------------------------------------

def _plan() -> RoutePlan:
    return RoutePlan(
        company="Acme", url="https://acme.example/careers", provider="unknown",
        method=router.METHOD_BROWSER, source=router.SOURCE_LIVE_PAGE,
    )


def _rows(count: int) -> list[dict]:
    return [
        {"company": "Acme", "title": f"Engineer {i}",
         "job_url": f"https://acme.example/jobs/{i}"}
        for i in range(count)
    ]


def _silence_other_tiers(monkeypatch):
    from ats.base import CollectorUnavailable

    def _unavailable(plan):
        raise CollectorUnavailable("not this tier")

    monkeypatch.setattr(router, "collect_via_jsonld", _unavailable)
    monkeypatch.setattr(router, "collect_via_framework_data", _unavailable)


def test_an_incomplete_cheap_harvest_does_not_end_the_ladder(monkeypatch):
    """Ten rows clears the floor - but the page said there were more."""
    _silence_other_tiers(monkeypatch)
    monkeypatch.setattr(router, "collect_via_static_html", lambda plan: CollectionResult(
        jobs=_rows(10), complete=False, reported_total=500,
        stop_reason=STOP_MORE_AVAILABLE,
    ))
    browser_ran = []

    def _browser(plan):
        browser_ran.append(True)
        return router.BrowserHarvest(records=_rows(480))

    monkeypatch.setattr(router, "collect_via_browser", _browser)

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert browser_ran, "the browser must still get its chance to paginate"
    assert len(result.jobs) == 480
    assert result.complete is True


def test_a_complete_cheap_harvest_still_short_circuits(monkeypatch):
    """The cheap tiers exist to avoid paying for a browser; that must survive."""
    _silence_other_tiers(monkeypatch)
    monkeypatch.setattr(router, "collect_via_static_html", lambda plan: CollectionResult(
        jobs=_rows(25), complete=True,
    ))

    def _browser(plan):  # pragma: no cover - must never run
        raise AssertionError("the browser was paid for despite a complete harvest")

    monkeypatch.setattr(router, "collect_via_browser", _browser)

    result = router.fetch_company_jobs("Acme", plan=_plan())
    assert len(result.jobs) == 25
    assert result.complete is True


def test_an_incomplete_cheap_harvest_kept_over_the_browser_stays_incomplete(monkeypatch):
    """The rows are kept because they are the better answer; the caveat rides along.

    This is the path that would otherwise re-open the hole: preferring the
    cheap tier's rows used to assert ``complete=True`` unconditionally, which
    is exactly the claim that lets removal sync delete the pages behind them.
    """
    _silence_other_tiers(monkeypatch)
    monkeypatch.setattr(router, "collect_via_static_html", lambda plan: CollectionResult(
        jobs=_rows(10), complete=False, reported_total=500,
        stop_reason=STOP_MORE_AVAILABLE,
    ))
    monkeypatch.setattr(
        router, "collect_via_browser", lambda plan: router.BrowserHarvest(records=_rows(2)),
    )

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert len(result.jobs) == 10, "the fuller harvest should win"
    assert result.complete is False
    assert result.stop_reason == STOP_MORE_AVAILABLE
    assert result.reported_total == 500


def test_an_incomplete_cheap_harvest_survives_a_browser_crash(monkeypatch):
    _silence_other_tiers(monkeypatch)
    monkeypatch.setattr(router, "collect_via_static_html", lambda plan: CollectionResult(
        jobs=_rows(10), complete=False, stop_reason=STOP_MORE_AVAILABLE,
    ))

    def _browser(plan):
        raise RuntimeError("no chromium here")

    monkeypatch.setattr(router, "collect_via_browser", _browser)

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert result.success is True
    assert len(result.jobs) == 10
    assert result.complete is False, "a crashed browser cannot make page one whole"


def test_a_bare_list_from_a_cheap_tier_is_still_accepted(monkeypatch):
    """Migration shim: an unconverted tier returning a plain list keeps working."""
    _silence_other_tiers(monkeypatch)
    monkeypatch.setattr(router, "collect_via_static_html", lambda plan: _rows(25))

    def _browser(plan):  # pragma: no cover - must never run
        raise AssertionError("a complete bare-list harvest should short-circuit")

    monkeypatch.setattr(router, "collect_via_browser", _browser)

    result = router.fetch_company_jobs("Acme", plan=_plan())
    assert len(result.jobs) == 25
    assert result.complete is True


# --- the end-to-end consequence -------------------------------------------

def test_removal_sync_skips_a_company_whose_page_advertised_more(tmp_path):
    """The whole point: an incomplete harvest must not delete stored jobs."""
    from database import JobDatabase
    from pipeline import sync_completed_companies

    db_path = tmp_path / "jobs.db"
    stored = [
        {"job_id": f"acme-{i}", "job_url": f"https://acme.example/jobs/{i}",
         "company": "Acme", "title": f"Engineer {i}", "location": "Dallas, TX"}
        for i in range(50)
    ]
    with JobDatabase(db_path) as database:
        database.upsert_jobs(stored)
        assert len(database.company_ids("Acme")) == 50

        page_one = router.CompanyResult(
            company="Acme", jobs=stored[:10], plan=_plan(), success=True,
            complete=False, stop_reason=STOP_MORE_AVAILABLE, reported_total=50,
        )
        stats = sync_completed_companies([page_one], database)

        assert stats["removed"] == 0
        assert stats["skipped_incomplete"] == 1
        assert len(database.company_ids("Acme")) == 50, (
            "the 40 postings on pages we never fetched must still be there"
        )


def test_removal_sync_still_runs_for_a_complete_harvest(tmp_path):
    """The safety net must not become a blanket amnesty."""
    from database import JobDatabase
    from pipeline import sync_completed_companies

    db_path = tmp_path / "jobs.db"
    stored = [
        {"job_id": f"acme-{i}", "job_url": f"https://acme.example/jobs/{i}",
         "company": "Acme", "title": f"Engineer {i}", "location": "Dallas, TX"}
        for i in range(50)
    ]
    with JobDatabase(db_path) as database:
        database.upsert_jobs(stored)
        complete = router.CompanyResult(
            company="Acme", jobs=stored[:10], plan=_plan(), success=True,
            complete=True,
        )
        stats = sync_completed_companies([complete], database)
        assert stats["synced"] == 1
        assert stats["skipped_incomplete"] == 0
