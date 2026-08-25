"""The JSON-LD tier must not end the ladder on a landing page's featured jobs.

``fetch_company_jobs`` accepted any non-zero JSON-LD harvest as the company's
complete job list and returned before Playwright ever ran. A careers landing
page that embeds three featured roles for SEO therefore reported three jobs for
an employer with thousands - the exact "landing page mistaken for the job list"
failure the browser path's ``hop_good_enough_rows`` floor exists to prevent,
happening one tier earlier where no floor was applied.

The tier was also gated on ``provider == UNKNOWN``, so a *known* provider whose
collector raised CollectorUnavailable skipped this cheap tier entirely and paid
for a browser - the case where it is most valuable.
"""

import pytest

import ats.router as router
from ats.base import CollectorUnavailable
from browser.playwright_scraper import PlaywrightResult


def _plan(provider=router.UNKNOWN):
    return router.RoutePlan(
        company="Acme", url="https://careers.acme.com/", provider=provider,
        method=router.METHOD_BROWSER, source=router.SOURCE_LIVE_PAGE,
        detection={"provider": provider, "url": "https://careers.acme.com/"},
    )


def _jobs(n, prefix="jsonld"):
    return [{"company": "Acme", "title": f"Data Engineer {i}",
             "job_url": f"https://careers.acme.com/{prefix}/{i}",
             "ats_provider": "jsonld", "scraping_method": "direct_api"}
            for i in range(n)]


def test_a_thin_jsonld_harvest_does_not_stop_the_ladder(monkeypatch):
    """Three featured roles must not be accepted as the whole job list."""
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: _jobs(3))
    browser_called = []

    def _browser(plan):
        browser_called.append(plan.company)
        return _jobs(40, prefix="browser"), None, None, False

    monkeypatch.setattr(router, "collect_via_browser", _browser)

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert browser_called == ["Acme"], "Playwright was never reached"
    assert len(result.jobs) == 40


def test_a_substantial_jsonld_harvest_still_short_circuits(monkeypatch):
    """A real list is worth taking over a browser render."""
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: _jobs(25))

    def _browser(plan):
        raise AssertionError("Playwright should not run for a full JSON-LD list")

    monkeypatch.setattr(router, "collect_via_browser", _browser)

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert result.success is True
    assert len(result.jobs) == 25


def test_a_thin_harvest_is_kept_when_the_browser_finds_nothing(monkeypatch):
    """Three real jobs beat zero - the fallback must not be discarded."""
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: _jobs(3))
    monkeypatch.setattr(router, "collect_via_browser", lambda plan: ([], None, None, False))

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert len(result.jobs) == 3
    assert result.success is True


def test_a_known_provider_whose_collector_failed_still_tries_jsonld(monkeypatch):
    """The tier was gated on UNKNOWN, skipping exactly the case it helps most."""
    def _api(plan):
        raise CollectorUnavailable("Workday CXS unavailable")

    monkeypatch.setattr(router, "collect_via_api", _api)
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: _jobs(30))
    monkeypatch.setattr(
        router, "collect_via_browser",
        lambda plan: (_ for _ in ()).throw(AssertionError("browser should not run")),
    )

    plan = _plan(provider="workday")
    plan.method = router.METHOD_API

    result = router.fetch_company_jobs("Acme", plan=plan)

    assert len(result.jobs) == 30
    assert result.fell_back is True
