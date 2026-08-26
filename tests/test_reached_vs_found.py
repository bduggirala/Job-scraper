""""We reached the site and it had nothing" is not a failure.

The browser path set ``success=bool(jobs)``, so a company that rendered
cleanly and genuinely had no matching openings was recorded identically to one
the scraper could not reach. That inflated the failure count, filled
scraper_failures.csv with rows needing no action, and wrote a misleading
``Data Retrieved = FALSE`` into the workbook.

Removal safety must not regress in the process: a zero-job result still has to
be skipped by the removal sync, because "we saw nothing" is never evidence
that every posting closed.
"""

import pytest

import ats.router as router
import pipeline
from database import JobDatabase


def _plan():
    return router.RoutePlan(
        company="Acme", url="https://careers.acme.test/", provider=router.UNKNOWN,
        method=router.METHOD_BROWSER, source=router.SOURCE_LIVE_PAGE,
    )


def test_a_clean_render_with_no_jobs_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: [])
    monkeypatch.setattr(router, "collect_via_browser", lambda plan: ([], None, None, False))

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert result.success is True
    assert result.error_type is None
    assert result.jobs == []


def test_a_blocked_site_is_still_a_failure(monkeypatch):
    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: [])
    monkeypatch.setattr(router, "collect_via_browser", lambda plan: ([], None, None, True))

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert result.success is False
    assert result.error_type == "AccessDenied"


def test_a_navigation_error_is_still_a_failure(monkeypatch):
    def boom(plan):
        raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    monkeypatch.setattr(router, "collect_via_jsonld", lambda plan: [])
    monkeypatch.setattr(router, "collect_via_browser", boom)

    result = router.fetch_company_jobs("Acme", plan=_plan())

    assert result.success is False
    assert result.error_type == "RuntimeError"


def test_a_zero_job_success_never_triggers_removal(tmp_path):
    """The fail-safe that must survive: no jobs is not 'all jobs closed'."""
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([
            {"job_id": "acme:x:1", "job_url": "https://x.test/1",
             "company": "Acme", "title": "Data Engineer"},
        ])

        empty = router.CompanyResult(
            company="Acme", jobs=[], plan=_plan(), success=True, complete=True,
        )
        pipeline.sync_completed_companies([empty], db)
        pipeline.sync_completed_companies([empty], db)

        assert db.company_ids("Acme") == {"acme:x:1"}


def test_a_zero_job_company_stays_out_of_the_failure_report(tmp_path, monkeypatch):
    from settings import load_settings

    cfg = load_settings()
    monkeypatch.setattr(cfg, "resolve_path", lambda *a, **kw: tmp_path)

    reached_empty = router.CompanyResult(
        company="Acme", jobs=[], plan=_plan(), success=True,
    )
    genuinely_failed = router.CompanyResult(
        company="Contoso", jobs=[], plan=_plan(), success=False,
        error_type="AccessDenied", error_message="challenge",
    )

    pipeline.write_outputs([], [reached_empty, genuinely_failed], cfg)

    import pandas as pd
    failures = pd.read_csv(tmp_path / "scraper_failures.csv")
    assert list(failures["company"]) == ["Contoso"]
