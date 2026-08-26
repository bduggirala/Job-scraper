"""Offline tests for two write-back gaps in the router:

1. ``plan_route`` must record the pre-repair URL and whether a repair
   happened, so a verified repair can be written back (see
   ``export_ats_urls.write_repaired_urls`` and ``pipeline.py``).
2. A provider found via ``resolve_from_page`` (an HTTP-only page resolution,
   as opposed to Playwright's browser-based self-heal) must be reported as a
   verified discovery once it actually returns jobs, so the existing
   ``write_discovered_urls`` write-back picks it up too.
"""

import ats.router as router
from ats.base import CollectionResult


def test_plan_route_records_raw_url_and_repair_flag(monkeypatch):
    monkeypatch.setattr(
        router, "repair_careers_url",
        lambda company, url: "https://www.nttdata.com/en-us/careers",
    )
    monkeypatch.setattr(
        router, "detect_ats",
        lambda url: {"provider": router.UNKNOWN, "url": url, "host": None,
                      "tenant": None, "site": None, "identifier": None},
    )
    monkeypatch.setattr(
        router, "resolve_from_page",
        lambda company, url: {"provider": router.UNKNOWN},
    )

    plan = router.plan_route(
        "NTT DATA", ats_url=None, live_jobs_url="https://careers.nttdata.com/",
        playwright_enabled=True,
    )

    assert plan.raw_url == "https://careers.nttdata.com/"
    assert plan.was_repaired is True
    assert plan.url == "https://www.nttdata.com/en-us/careers"
    assert plan.source == router.SOURCE_LIVE_PAGE


def test_plan_route_no_repair_when_url_already_live(monkeypatch):
    monkeypatch.setattr(router, "repair_careers_url", lambda company, url: None)
    monkeypatch.setattr(
        router, "detect_ats",
        lambda url: {"provider": "workday", "url": url, "host": "x.wd1.myworkdayjobs.com",
                      "tenant": "x", "site": "External", "identifier": "External"},
    )

    plan = router.plan_route(
        "Acme", ats_url="https://x.wd1.myworkdayjobs.com/External", playwright_enabled=True,
    )

    assert plan.was_repaired is False
    assert plan.raw_url == "https://x.wd1.myworkdayjobs.com/External"
    assert plan.url == plan.raw_url


def test_page_resolved_api_success_is_reported_as_verified_discovery(monkeypatch):
    """A resolve_from_page hit that actually returns jobs must be write-back-eligible."""
    plan = router.RoutePlan(
        company="Primoris Services",
        url="https://prim.wd1.myworkdayjobs.com/External",
        provider="workday",
        method=router.METHOD_API,
        source=router.SOURCE_LIVE_PAGE,
        resolved_via_page=True,
        raw_url="https://www.prim.com/careers",
        was_repaired=True,
    )
    monkeypatch.setattr(
        router, "collect_via_api",
        lambda p: CollectionResult(jobs=[{"title": "Data Engineer"}]),
    )

    result = router.fetch_company_jobs("Primoris Services", plan=plan)

    assert result.success is True
    assert result.discovered_ats_url == "https://prim.wd1.myworkdayjobs.com/External"
    assert result.discovered_provider == "workday"
    assert result.discovery_verified is True


def test_direct_lexical_api_success_is_not_reported_as_discovery(monkeypatch):
    """A provider recognized straight from the workbook URL needs no write-back."""
    plan = router.RoutePlan(
        company="Capital One",
        url="https://capitalone.wd12.myworkdayjobs.com/Capital_One",
        provider="workday",
        method=router.METHOD_API,
        source=router.SOURCE_ATS_URL,
        resolved_via_page=False,
    )
    monkeypatch.setattr(
        router, "collect_via_api",
        lambda p: CollectionResult(jobs=[{"title": "Data Engineer"}]),
    )

    result = router.fetch_company_jobs("Capital One", plan=plan)

    assert result.success is True
    assert result.discovered_ats_url is None
    assert result.discovery_verified is False
