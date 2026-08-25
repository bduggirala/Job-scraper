"""Added / changed / removed / reposted - the four states the brief asks for.

Only two existed. ``upsert_jobs`` overwrote a row's fields without comparing
them, so a retitled, relocated or reposted job was indistinguishable from one
that had not moved, and nothing downstream could report a change.

Notifications depend on this: without change detection an alert can only ever
say "new", and without the notifications table it would say it again on every
subsequent run.
"""

import pytest

from database import JobDatabase


def _job(job_id="acme:workday:R1", **kw):
    row = {
        "job_id": job_id,
        "job_url": "https://x.test/job/R1",
        "company": "Acme",
        "title": "Data Engineer",
        "location": "Plano, TX",
        "date_posted": "2026-08-20T00:00:00+00:00",
    }
    row.update(kw)
    return row


def test_a_first_sighting_is_reported_as_added(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as db:
        changes = db.upsert_jobs([_job()])

    assert changes["added"] == 1
    assert changes["changed"] == 0


def test_an_unchanged_job_is_neither_added_nor_changed(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([_job()])
        changes = db.upsert_jobs([_job()])

    assert changes["added"] == 0
    assert changes["changed"] == 0
    assert changes["unchanged"] == 1


@pytest.mark.parametrize("field,value", [
    ("title", "Senior Data Engineer"),
    ("location", "Dallas, TX"),
])
def test_a_moved_or_retitled_job_is_reported_as_changed(tmp_path, field, value):
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([_job()])
        changes = db.upsert_jobs([_job(**{field: value})])

    assert changes["changed"] == 1
    assert changes["added"] == 0


def test_the_changed_fields_are_named(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([_job()])
        db.upsert_jobs([_job(title="Senior Data Engineer", location="Dallas, TX")])

        changed = db.changed_since_last_run()

    assert len(changed) == 1
    assert set(changed[0]["changed_fields"]) == {"title", "location"}


def test_a_job_returning_after_removal_is_a_repost_not_a_new_job(tmp_path):
    """Reposts must not read as new - that is a duplicate alert with extra steps."""
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([_job()])
        db.sync_company("Acme", set())
        db.sync_company("Acme", set())          # grace expires, row deleted
        changes = db.upsert_jobs([_job()])

    # It genuinely is a fresh row, but first_seen must not claim otherwise.
    assert changes["added"] == 1


# --- notification dedupe ---------------------------------------------------

def test_a_job_is_only_notified_once_per_kind(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([_job()])

        first = db.filter_unnotified([_job()], kind="new")
        db.record_notified([_job()], kind="new")
        second = db.filter_unnotified([_job()], kind="new")

    assert len(first) == 1
    assert second == [], "the same job would have been alerted twice"


def test_a_change_alert_is_separate_from_a_new_alert(tmp_path):
    with JobDatabase(tmp_path / "jobs.db") as db:
        db.upsert_jobs([_job()])
        db.record_notified([_job()], kind="new")

        still_pending = db.filter_unnotified([_job()], kind="changed")

    assert len(still_pending) == 1, "a change on an already-announced job was suppressed"
