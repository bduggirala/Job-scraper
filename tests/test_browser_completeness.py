"""A browser scrape that hit the page cap must not claim it saw everything.

``CollectionResult.complete`` was built so a partial harvest could never be
read as "the jobs we did not reach have closed" - but only the direct-API path
was ever wired to it. The browser path computes exactly the signal it needs:
``_paginate_and_extract`` returns an ``exhausted`` flag and
``playwright_scraper`` even logs "pagination stopped at the N-page cap with
more results still available". That flag was then dropped on the floor at all
three call sites, so every browser company reported ``complete=True``.

The consequence is the original bug, on the path that most needs the guard:
``playwright.max_pages`` is 10, so an employer with 30 pages of listings has
pages 11+ absent from the harvest, and ``sync_completed_companies`` ages them
out of the tracker - destroying ``first_seen`` and re-reporting them as new
when the cap next lands differently.
"""

import pytest

import ats.router as router
from ats.base import STOP_BUDGET


class _Result:
    """Stand-in for browser.playwright_scraper.PlaywrightResult."""

    def __init__(self, jobs, complete=True, stop_reason=None):
        self.jobs = jobs
        self.discovered_ats_url = None
        self.discovered_provider = None
        self.queries_run = []
        self.blocked = False
        self.complete = complete
        self.stop_reason = stop_reason


def _plan():
    return router.RoutePlan(
        company="Acme", url="https://careers.acme.test/jobs", provider="unknown",
        method=router.METHOD_BROWSER, source=router.SOURCE_ATS_URL,
        detection={"provider": "unknown", "url": "https://careers.acme.test/jobs"},
    )


def _rows(n):
    return [{"title": f"Data Engineer {i}", "location": "Dallas, TX",
             "job_url": f"https://careers.acme.test/jobs/{1000 + i}"} for i in range(n)]


@pytest.fixture(autouse=True)
def _no_cheap_tiers(monkeypatch):
    from ats.base import CollectorUnavailable
    for name in ("collect_via_jsonld", "collect_via_static_html",
                 "collect_via_framework_data"):
        monkeypatch.setattr(
            router, name,
            lambda plan: (_ for _ in ()).throw(CollectorUnavailable("n/a")),
        )


def test_a_capped_browser_scrape_is_reported_incomplete(monkeypatch):
    monkeypatch.setattr(
        "browser.playwright_scraper.scrape_with_playwright",
        lambda company, url: _Result(_rows(200), complete=False,
                                     stop_reason=STOP_BUDGET),
    )

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert result.success is True
    assert len(result.jobs) == 200
    assert result.complete is False, (
        "browser hit its page cap but the company was marked complete, so "
        "removal sync would delete every job past the cap"
    )
    assert result.stop_reason == STOP_BUDGET


def test_a_browser_scrape_that_ran_out_of_pages_is_complete(monkeypatch):
    monkeypatch.setattr(
        "browser.playwright_scraper.scrape_with_playwright",
        lambda company, url: _Result(_rows(12), complete=True),
    )

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert result.success is True
    assert result.complete is True
    assert result.stop_reason is None


def test_an_incomplete_browser_company_is_not_removal_synced(monkeypatch, tmp_path):
    """The end-to-end consequence, not just the flag."""
    from database import JobDatabase
    from pipeline import sync_completed_companies

    monkeypatch.setattr(
        "browser.playwright_scraper.scrape_with_playwright",
        lambda company, url: _Result(_rows(3), complete=False,
                                     stop_reason=STOP_BUDGET),
    )
    result = router.fetch_company_jobs("Acme", plan=_plan())
    for job in result.jobs:
        job["job_id"] = f"acme:{job['job_url'].rsplit('/', 1)[-1]}"

    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([
            {"job_id": f"acme:{i}", "job_url": f"https://careers.acme.test/jobs/{i}",
             "company": "Acme", "title": "Data Engineer", "location": "Dallas, TX"}
            for i in (1000, 1001, 1002, 9998, 9999)
        ])
        stats = sync_completed_companies([result], db)
        remaining = db.company_ids("Acme")

    assert stats["skipped_incomplete"] == 1
    assert "acme:9999" in remaining, "a capped scrape aged out unreached postings"
    assert stats["removed"] == 0
