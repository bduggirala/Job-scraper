"""The removal-safety contract: an incomplete scrape must never delete jobs.

This is the defect that motivated the whole CollectionResult change. A page
failing partway through pagination used to produce a partial harvest that the
router reported as a success, after which ``pipeline.run()`` called
``database.sync_company()`` and hard-deleted every stored job absent from the
partial set - reading one transient HTTP error as "all those postings closed".

The tests here span the three layers that have to agree for that to be safe:
the collector says it is incomplete, the router carries that flag, and the
pipeline refuses to sync on it.
"""

import pytest

import ats.router as router
from ats.base import STOP_PAGE_FAILED, ATSCollector, CollectionResult
from database import JobDatabase


# --- layer 1: the router coerces and carries `complete` --------------------

class _PartialCollector(ATSCollector):
    provider = "workday"

    def collect(self) -> CollectionResult:
        return CollectionResult(
            jobs=[{"title": "Data Engineer", "job_url": "https://x.test/job/1"}],
            complete=False, pages_fetched=2, reported_total=200,
            stop_reason=STOP_PAGE_FAILED,
        )


class _LegacyListCollector(ATSCollector):
    """A collector not yet converted - still returns a bare list."""
    provider = "greenhouse"

    def collect(self):
        return [{"title": "Data Engineer", "job_url": "https://x.test/job/2"}]


def _plan(provider: str) -> router.RoutePlan:
    return router.RoutePlan(
        company="Acme", url="https://x.test/", provider=provider,
        method=router.METHOD_API, source=router.SOURCE_ATS_URL,
        detection={"provider": provider, "url": "https://x.test/"},
    )


def test_an_incomplete_collection_reaches_the_company_result(monkeypatch):
    monkeypatch.setitem(router.COLLECTORS, "workday", _PartialCollector)

    result = router.fetch_company_jobs("Acme", plan=_plan("workday"))

    assert result.success is True          # we did get real jobs
    assert result.complete is False        # but not all of them
    assert len(result.jobs) == 1


def test_an_unconverted_collector_returning_a_bare_list_is_treated_as_complete(monkeypatch):
    monkeypatch.setitem(router.COLLECTORS, "greenhouse", _LegacyListCollector)

    result = router.fetch_company_jobs("Acme", plan=_plan("greenhouse"))

    assert result.success is True
    assert result.complete is True
    assert result.jobs[0]["title"] == "Data Engineer"


# --- layer 2: the database honours a grace period --------------------------

def _seed(db: JobDatabase, company: str, job_ids: list[str]) -> None:
    db.upsert_jobs([
        {"job_id": jid, "job_url": f"https://x.test/job/{jid}",
         "company": company, "title": "Data Engineer", "location": "Dallas, TX"}
        for jid in job_ids
    ])


def test_sync_company_still_removes_jobs_once_the_grace_period_expires(tmp_path):
    """The safety net must not break the behaviour it is protecting.

    Removal still happens - it just takes two consecutive complete scrapes
    that both miss the job, rather than one.
    """
    with JobDatabase(tmp_path / "jobs.db") as db:
        _seed(db, "Acme", ["a", "b", "c"])

        db.sync_company("Acme", {"a", "b"})
        stats = db.sync_company("Acme", {"a", "b"})

        assert stats["removed"] == 1
        assert db.company_ids("Acme") == {"a", "b"}


def test_a_job_missing_from_one_scrape_survives_until_the_grace_period_expires(tmp_path):
    """A single flicker must not destroy first_seen and re-trigger 'new'."""
    with JobDatabase(tmp_path / "jobs.db") as db:
        _seed(db, "Acme", ["a", "b"])

        first = db.sync_company("Acme", {"a"})
        assert first["removed"] == 0, "removed on the very first miss"
        assert "b" in db.company_ids("Acme")

        second = db.sync_company("Acme", {"a"})
        assert second["removed"] == 1
        assert db.company_ids("Acme") == {"a"}


def test_a_reappearing_job_clears_its_miss_counter(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as db:
        _seed(db, "Acme", ["a", "b"])

        db.sync_company("Acme", {"a"})      # b missed once
        _seed(db, "Acme", ["a", "b"])       # b is back
        db.sync_company("Acme", {"a"})      # b missed once again, not twice

        assert "b" in db.company_ids("Acme")


# --- layer 3: the pipeline refuses to sync an incomplete company -----------

def test_pipeline_does_not_sync_a_company_whose_scrape_was_incomplete(tmp_path, monkeypatch):
    """The end-to-end guarantee: stored jobs survive a partial scrape."""
    import pipeline

    with JobDatabase(tmp_path / "jobs.db") as db:
        _seed(db, "Acme", ["keep-1", "keep-2", "keep-3"])

    synced: list[str] = []
    original_sync = JobDatabase.sync_company

    def _spy(self, company, current_ids):
        synced.append(company)
        return original_sync(self, company, current_ids)

    monkeypatch.setattr(JobDatabase, "sync_company", _spy)

    incomplete = router.CompanyResult(
        company="Acme",
        jobs=[{"job_id": "keep-1", "job_url": "https://x.test/job/keep-1",
               "company": "Acme", "title": "Data Engineer"}],
        plan=_plan("workday"), success=True, complete=False,
    )

    pipeline.sync_completed_companies(
        [incomplete], JobDatabase(tmp_path / "jobs.db")
    )

    assert synced == [], "sync_company was called for an incomplete scrape"
    with JobDatabase(tmp_path / "jobs.db") as db:
        assert db.company_ids("Acme") == {"keep-1", "keep-2", "keep-3"}


def test_pipeline_syncs_a_company_whose_scrape_was_complete(tmp_path, monkeypatch):
    import pipeline

    with JobDatabase(tmp_path / "jobs.db") as db:
        _seed(db, "Acme", ["keep-1", "gone-1"])

    complete = router.CompanyResult(
        company="Acme",
        jobs=[{"job_id": "keep-1", "job_url": "https://x.test/job/keep-1",
               "company": "Acme", "title": "Data Engineer"}],
        plan=_plan("workday"), success=True, complete=True,
    )

    with JobDatabase(tmp_path / "jobs.db") as db:
        pipeline.sync_completed_companies([complete], db)
        # First miss is a grace period, second removes.
        pipeline.sync_completed_companies([complete], db)
        assert db.company_ids("Acme") == {"keep-1"}
