"""A database keyed on an older job-id scheme must be cleared, not merged.

When the id format changes, every stored id becomes unmatchable: nothing in a
new run will ever equal an old row's ``job_id``. Left in place, those rows sit
in the table forever - never refreshed, never removed (removal only considers
ids the current run produced), and inflating ``known_ids()`` so genuinely new
jobs are compared against dead identities.

Clearing is the honest option, and it is cheap here: ``first_seen`` is the only
history the table holds, and it is already unrecoverable once the ids change.
"""

import sqlite3


from database import JobDatabase
from job_identity import JOB_ID_SCHEME_VERSION


def _rows(path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()


def _seed(db: JobDatabase, job_ids: list[str]) -> None:
    db.upsert_jobs([
        {"job_id": jid, "job_url": f"https://x.test/job/{jid}",
         "company": "Acme", "title": "Data Engineer"}
        for jid in job_ids
    ])


def test_a_fresh_database_records_the_current_scheme_version(tmp_path):
    path = tmp_path / "jobs.db"
    with JobDatabase(path) as db:
        assert db.scheme_version() == JOB_ID_SCHEME_VERSION


def test_reopening_at_the_same_scheme_version_keeps_the_rows(tmp_path):
    path = tmp_path / "jobs.db"
    with JobDatabase(path) as db:
        _seed(db, ["acme:workday:R1", "acme:workday:R2"])

    with JobDatabase(path) as db:
        assert db.company_ids("Acme") == {"acme:workday:R1", "acme:workday:R2"}


def test_an_older_scheme_version_clears_the_table(tmp_path):
    """The v1 -> v2 case: ids gained a company prefix, so none can ever match."""
    path = tmp_path / "jobs.db"
    with JobDatabase(path) as db:
        _seed(db, ["workday:R1", "workday:R2"])       # v1-shaped ids
        db._set_scheme_version(1)                      # pretend it was written by v1

    assert _rows(path) == 2

    with JobDatabase(path) as db:
        assert db.company_ids("Acme") == set()
        assert db.scheme_version() == JOB_ID_SCHEME_VERSION

    assert _rows(path) == 0


def test_a_database_predating_version_tracking_is_cleared(tmp_path):
    """No meta table at all means it was written before this existed."""
    path = tmp_path / "jobs.db"
    with JobDatabase(path) as db:
        _seed(db, ["workday:R1"])

    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE meta")
    conn.commit()
    conn.close()

    with JobDatabase(path) as db:
        assert db.company_ids("Acme") == set()
        assert db.scheme_version() == JOB_ID_SCHEME_VERSION
