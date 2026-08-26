"""A failed send must not lose the jobs it was going to announce.

``send_notifications`` records jobs as notified only after a successful send,
and its comment says why: "doing it before would let one SMTP failure suppress
those jobs permanently". The bookkeeping is right, but the *candidate set* was
not - it was ``[j for j in final_jobs if j["is_new"]]``, and ``is_new`` is
computed against the ids already in the database.

By the time a send is attempted the jobs have been upserted, so on the next run
they are no longer new, never enter the candidate set, and are never announced -
however faithfully the notifications table records that they were not. One SMTP
hiccup therefore lost that run's alerts for good, which is exactly the outcome
the guard was written to prevent.

Selecting on "matching and never announced" instead makes the retry automatic:
this run's new jobs and any earlier run's unannounced ones are the same query.
"""

import pytest

from database import JobDatabase
from pipeline import unannounced_matching_jobs


@pytest.fixture
def db(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as database:
        yield database


def _job(n, is_new=False):
    return {"job_id": f"acme:{n}", "job_url": f"https://acme.test/jobs/{n}",
            "company": "Acme", "title": "Data Engineer", "location": "Dallas, TX",
            "is_new": is_new}


def test_this_runs_new_jobs_are_candidates(db):
    jobs = [_job(1, is_new=True), _job(2, is_new=True)]
    db.upsert_jobs(jobs)

    assert {j["job_id"] for j in unannounced_matching_jobs(db, jobs)} == {
        "acme:1", "acme:2"}


def test_a_job_already_announced_is_not_a_candidate(db):
    jobs = [_job(1, is_new=True), _job(2, is_new=True)]
    db.upsert_jobs(jobs)
    db.record_notified([_job(1)], kind="new")

    assert [j["job_id"] for j in unannounced_matching_jobs(db, jobs)] == ["acme:2"]


def test_a_job_missed_by_a_failed_send_is_retried_next_run(db):
    """The bug: after the upsert it is no longer new, so it was never retried."""
    jobs = [_job(1, is_new=True)]
    db.upsert_jobs(jobs)
    # ...send fails, nothing recorded...

    # Next run: the same job is present but no longer new.
    next_run = [_job(1, is_new=False)]
    db.upsert_jobs(next_run)

    candidates = unannounced_matching_jobs(db, next_run)
    assert [j["job_id"] for j in candidates] == ["acme:1"], (
        "a job whose announcement failed was silently dropped forever"
    )


def test_a_successful_send_stops_the_retry(db):
    jobs = [_job(1, is_new=True)]
    db.upsert_jobs(jobs)
    db.record_notified(unannounced_matching_jobs(db, jobs), kind="new")

    assert unannounced_matching_jobs(db, [_job(1, is_new=False)]) == []


def test_a_long_standing_job_is_not_announced_repeatedly(db):
    """The steady state: nothing new, nothing to say."""
    jobs = [_job(n, is_new=True) for n in range(1, 6)]
    db.upsert_jobs(jobs)
    db.record_notified(unannounced_matching_jobs(db, jobs), kind="new")

    for _ in range(3):
        same = [_job(n) for n in range(1, 6)]
        db.upsert_jobs(same)
        assert unannounced_matching_jobs(db, same) == []
