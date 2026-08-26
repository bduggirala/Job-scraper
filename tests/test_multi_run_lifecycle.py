"""The five-run lifecycle, end to end against a real SQLite file.

Each unit of this is covered elsewhere; what this adds is the sequence, which
is where the interesting failures live. A grace period that works in isolation
still deletes a job if an incomplete run is allowed to increment its miss
counter, and a notification filter that works on one run still repeats itself
on the next if the digest is rebuilt from a different set than it records.

    run 1  three new jobs               -> stored, all new, all announced
    run 2  the same three               -> unchanged, nothing re-announced
    run 3  one edited, one added,
           one gone from the source     -> changed / new / one miss, no delete
    run 4  the source scrape fails
           partway through              -> nothing removed, counters untouched
    run 5  a clean run, still missing    -> grace expires, removed

``sync_completed_companies`` is the unit under test throughout, because it is
the one place where "we did not see it" turns into "it is gone".
"""

import pytest

from ats.base import STOP_PAGE_FAILED
from ats.router import CompanyResult, RoutePlan
from database import REMOVAL_GRACE_MISSES, JobDatabase
from pipeline import sync_completed_companies

COMPANY = "Acme"


def _plan():
    return RoutePlan(company=COMPANY, url="https://acme.test/jobs",
                     provider="greenhouse", method="direct_api", source="ats_url")


def _job(n, title="Data Engineer", location="Dallas, TX"):
    return {"job_id": f"acme:{n}", "job_url": f"https://acme.test/jobs/{n}",
            "company": COMPANY, "title": title, "location": location,
            "date_posted": "2026-08-20"}


def _result(jobs, *, complete=True, success=True, stop_reason=None):
    return CompanyResult(company=COMPANY, jobs=jobs, plan=_plan(), success=success,
                         complete=complete, stop_reason=stop_reason)


@pytest.fixture
def db(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as database:
        yield database


def test_the_five_run_lifecycle(db):
    # --- run 1: three new jobs -------------------------------------------
    jobs = [_job(1), _job(2), _job(3)]
    counts = db.upsert_jobs(jobs)
    sync_completed_companies([_result(jobs)], db)

    assert counts == {"added": 3, "changed": 0, "unchanged": 0}
    assert db.company_ids(COMPANY) == {"acme:1", "acme:2", "acme:3"}

    announced = db.filter_unnotified(jobs, kind="new")
    assert len(announced) == 3
    db.record_notified(announced, kind="new")
    first_seen = db.get_first_seen_map(["acme:1"])["acme:1"]

    # --- run 2: identical source -----------------------------------------
    counts = db.upsert_jobs([_job(1), _job(2), _job(3)])
    sync_completed_companies([_result([_job(1), _job(2), _job(3)])], db)

    assert counts == {"added": 0, "changed": 0, "unchanged": 3}
    assert db.filter_unnotified([_job(1), _job(2), _job(3)], kind="new") == [], (
        "run 2 would have re-announced jobs already sent"
    )
    assert db.get_first_seen_map(["acme:1"])["acme:1"] == first_seen, (
        "first_seen must never be rewritten"
    )
    assert db.changed_since_last_run() == []

    # --- run 3: one edited, one added, one gone ---------------------------
    edited = _job(2, title="Senior Data Engineer")
    added = _job(4)
    seen = [_job(1), edited, added]                       # job 3 absent

    counts = db.upsert_jobs(seen)
    stats = sync_completed_companies([_result(seen)], db)

    assert counts == {"added": 1, "changed": 1, "unchanged": 1}

    changed = db.changed_since_last_run()
    assert [c["job_id"] for c in changed] == ["acme:2"]
    assert changed[0]["changed_fields"] == ["title"]

    assert stats["removed"] == 0, "a job missing once must not be deleted"
    assert "acme:3" in db.company_ids(COMPANY)

    new_only = db.filter_unnotified(seen, kind="new")
    assert [j["job_id"] for j in new_only] == ["acme:4"]
    db.record_notified(new_only, kind="new")
    db.record_notified(changed, kind="changed")
    db.clear_change_marks()

    # --- run 4: the scrape fails partway through --------------------------
    partial = [_job(1)]
    stats = sync_completed_companies(
        [_result(partial, complete=False, stop_reason=STOP_PAGE_FAILED)], db)

    assert stats["skipped_incomplete"] == 1
    assert stats["removed"] == 0
    assert db.company_ids(COMPANY) == {"acme:1", "acme:2", "acme:3", "acme:4"}, (
        "an incomplete scrape removed jobs it simply never reached"
    )

    # --- run 5: clean run, job 3 still absent -----------------------------
    seen = [_job(1), edited, added]
    db.upsert_jobs(seen)
    stats = sync_completed_companies([_result(seen)], db)

    assert stats["removed"] == 1, (
        f"job 3 has now missed {REMOVAL_GRACE_MISSES} consecutive complete "
        f"runs and should be gone"
    )
    assert db.company_ids(COMPANY) == {"acme:1", "acme:2", "acme:4"}


def test_an_incomplete_run_does_not_advance_the_grace_counter(db):
    """The subtle one: a failed run must not spend a job's grace period."""
    jobs = [_job(1), _job(2)]
    db.upsert_jobs(jobs)
    sync_completed_companies([_result(jobs)], db)

    for _ in range(5):
        sync_completed_companies(
            [_result([_job(1)], complete=False, stop_reason=STOP_PAGE_FAILED)], db)

    assert "acme:2" in db.company_ids(COMPANY), (
        "five incomplete runs aged out a job that was never confirmed absent"
    )

    # One clean run: still inside the grace period.
    db.upsert_jobs([_job(1)])
    sync_completed_companies([_result([_job(1)])], db)
    assert "acme:2" in db.company_ids(COMPANY)

    # Second clean run: now it goes.
    db.upsert_jobs([_job(1)])
    sync_completed_companies([_result([_job(1)])], db)
    assert "acme:2" not in db.company_ids(COMPANY)


def test_a_returning_job_keeps_its_original_first_seen(db):
    """Grace exists so a flicker does not re-report a job as new."""
    db.upsert_jobs([_job(1)])
    sync_completed_companies([_result([_job(1)])], db)
    original = db.get_first_seen_map(["acme:1"])["acme:1"]

    # One miss, then it comes back.
    sync_completed_companies([_result([_job(2)])], db)
    db.upsert_jobs([_job(1)])
    sync_completed_companies([_result([_job(1)])], db)

    assert db.get_first_seen_map(["acme:1"])["acme:1"] == original
    assert db.company_ids(COMPANY) == {"acme:1", "acme:2"}, (
        "the flicker cost the job its row, which is what destroys first_seen"
    )


def test_a_failed_company_is_never_synced(db):
    """success=False tells us nothing about what the employer still lists."""
    jobs = [_job(1), _job(2)]
    db.upsert_jobs(jobs)
    sync_completed_companies([_result(jobs)], db)

    for _ in range(4):
        sync_completed_companies([_result([], success=False)], db)

    assert db.company_ids(COMPANY) == {"acme:1", "acme:2"}


def test_a_zero_job_success_is_never_read_as_everything_closed(db):
    """Reaching a site that rendered nothing is not evidence of closure."""
    jobs = [_job(1), _job(2)]
    db.upsert_jobs(jobs)
    sync_completed_companies([_result(jobs)], db)

    for _ in range(4):
        sync_completed_companies([_result([])], db)

    assert db.company_ids(COMPANY) == {"acme:1", "acme:2"}
