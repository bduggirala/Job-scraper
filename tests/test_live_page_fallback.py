"""A dead ATS URL must not strand a company whose careers page still works.

When the workbook's ``ATS URL`` column points at a tenant that has since been
retired - an acquisition, a rebrand, an ATS migration - the collector correctly
raises CollectorUnavailable, and the browser fallback then re-rendered *the
same dead URL* and found nothing. Meanwhile the ``Live Jobs Page`` column often
holds a perfectly good careers site that was never tried.

Confirmed live: McAfee's Workday tenant answers ``total: 0`` and Walmart's
answers HTTP 422 with HTTP 500 on the page itself, while both companies have
(or in McAfee's case, list) a working careers page.
"""


import ats.router as router
from ats.base import CollectorUnavailable


def _plan(url, live=None, provider="workday"):
    return router.RoutePlan(
        company="McAfee", url=url, provider=provider,
        method=router.METHOD_API, source=router.SOURCE_ATS_URL,
        detection={"provider": provider, "url": url},
        live_jobs_url=live,
    )


def test_the_browser_falls_back_to_the_live_page_when_the_ats_url_is_dead(monkeypatch):
    rendered: list[str] = []

    def _api(plan):
        raise CollectorUnavailable("Workday CXS returned zero postings")

    def _browser(plan):
        rendered.append(plan.url)
        return router.BrowserHarvest()

    monkeypatch.setattr(router, "collect_via_api", _api)
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: [])
    monkeypatch.setattr(router, "collect_via_browser", _browser)

    plan = _plan("https://mcafee.wd1.myworkdayjobs.com/External/",
                 live="https://www.mcafee.com/en-us/careers.html")
    router.fetch_company_jobs("McAfee", plan=plan)

    assert "mcafee.com/en-us/careers.html" in rendered[-1], (
        f"browser re-rendered the dead ATS URL instead of the careers page: {rendered}"
    )


def test_the_dead_ats_url_is_still_tried_first(monkeypatch):
    """The careers page is a fallback, not a replacement - the ATS API is
    better whenever it works."""
    attempted: list[str] = []

    def _api(plan):
        attempted.append(plan.url)
        from ats.base import CollectionResult
        return CollectionResult(jobs=[{"title": "Data Engineer"}])

    monkeypatch.setattr(router, "collect_via_api", _api)

    plan = _plan("https://mcafee.wd1.myworkdayjobs.com/External/",
                 live="https://www.mcafee.com/en-us/careers.html")
    result = router.fetch_company_jobs("McAfee", plan=plan)

    assert attempted == ["https://mcafee.wd1.myworkdayjobs.com/External/"]
    assert result.success is True


def test_no_live_page_means_the_original_url_is_used(monkeypatch):
    """Walmart has no Live Jobs Page - behaviour must be unchanged for it."""
    rendered: list[str] = []

    monkeypatch.setattr(
        router, "collect_via_api",
        lambda plan: (_ for _ in ()).throw(CollectorUnavailable("dead")),
    )
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: [])
    monkeypatch.setattr(
        router, "collect_via_browser",
        lambda plan: (rendered.append(plan.url), router.BrowserHarvest())[1],
    )

    plan = _plan("https://walmart.wd5.myworkdayjobs.com/WalmartExternal/", live=None)
    router.fetch_company_jobs("Walmart", plan=plan)

    assert rendered[-1] == "https://walmart.wd5.myworkdayjobs.com/WalmartExternal/"


def test_plan_route_carries_the_live_page_alongside_the_ats_url():
    """Both columns must survive routing, not just the one that won."""
    plan = router.plan_route(
        "McAfee",
        ats_url="https://mcafee.wd1.myworkdayjobs.com/External/",
        live_jobs_url="https://www.mcafee.com/en-us/careers.html",
        resolve_pages=False,
    )

    assert plan.url == "https://mcafee.wd1.myworkdayjobs.com/External/"
    assert plan.live_jobs_url == "https://www.mcafee.com/en-us/careers.html"


def test_a_live_page_identical_to_the_ats_url_is_not_retried(monkeypatch):
    """No point rendering the same page twice."""
    rendered: list[str] = []

    monkeypatch.setattr(
        router, "collect_via_api",
        lambda plan: (_ for _ in ()).throw(CollectorUnavailable("dead")),
    )
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: [])
    monkeypatch.setattr(
        router, "collect_via_browser",
        lambda plan: (rendered.append(plan.url), router.BrowserHarvest())[1],
    )

    same = "https://careers.christushealth.org/job-search"
    router.fetch_company_jobs("CHRISTUS", plan=_plan(same, live=same))

    assert len(rendered) == 1
