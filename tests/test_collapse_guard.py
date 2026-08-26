"""A harvest that collapses against the previous run must not delete jobs.

``CollectionResult.complete`` is the collector's own account of itself, and it
is only as good as the collector's knowledge. A walk that *knows* it stopped
short reports it. A walk that never found the list cannot: the browser
traversal renders a careers site, lands on a "featured roles" panel instead of
the job list, extracts four rows, finds no pagination control, and concludes it
saw everything there was.

Measured live: Caterpillar collected 138 jobs on one run and 4 on the next,
both reported ``complete``. Nothing inside the second scrape could tell it was
wrong - but the first run could, and did: 143 stored postings were left one miss
from deletion.

The guard is deliberately one run of grace, not a permanent veto. It compares
against the *previous run's collected count* rather than the stored row count,
because nothing is ever deleted while the guard holds, so a database-based
comparison could never clear itself.
"""

from __future__ import annotations

import json

import pytest

from ats.router import CompanyResult, RoutePlan
from database import JobDatabase
from pipeline import (
    COLLAPSE_FLOOR,
    COLLAPSE_RATIO,
    collapsed_against,
    previous_job_counts,
    sync_completed_companies,
)


# --- the predicate ---------------------------------------------------------

@pytest.mark.parametrize("previous, collected, expected", [
    (138, 4, True),      # Caterpillar, the case this exists for
    (100, 49, True),     # just under half
    (100, 50, False),    # exactly half is tolerated
    (100, 99, False),    # ordinary churn
    (100, 140, False),   # growth is never a collapse
    (0, 0, False),       # nothing to compare against
    (None, 5, False),    # a company absent from the previous report
])
def test_collapse_predicate(previous, collected, expected):
    assert collapsed_against(previous, collected) is expected


def test_a_small_employer_is_exempt():
    """Going from 6 postings to 2 is an ordinary week, not a cliff."""
    assert COLLAPSE_FLOOR > 6
    assert collapsed_against(6, 2) is False


def test_the_floor_is_where_the_ratio_starts_applying():
    assert collapsed_against(COLLAPSE_FLOOR - 1, 0) is False
    assert collapsed_against(COLLAPSE_FLOOR, 0) is True


def test_the_ratio_is_a_halving():
    assert COLLAPSE_RATIO == 0.5


# --- reading the previous report -------------------------------------------

def test_previous_counts_are_read_from_the_report(tmp_path):
    report = tmp_path / "last_run.json"
    report.write_text(json.dumps({"companies": [
        {"company": "Caterpillar", "jobs": 138},
        {"company": "Acme", "jobs": 12},
        {"company": "NoCount"},
    ]}), encoding="utf-8")

    counts = previous_job_counts(report)
    assert counts == {"Caterpillar": 138, "Acme": 12}


def test_a_missing_report_disables_the_guard(tmp_path):
    """A first run has nothing to compare against and must still sync."""
    assert previous_job_counts(tmp_path / "nope.json") == {}


def test_an_unreadable_report_disables_the_guard(tmp_path):
    report = tmp_path / "last_run.json"
    report.write_text("{ not json", encoding="utf-8")
    assert previous_job_counts(report) == {}


# --- the consequence -------------------------------------------------------

def _plan() -> RoutePlan:
    return RoutePlan(
        company="Caterpillar", url="https://cat.example/careers",
        provider="unknown", method="playwright", source="live_jobs_page",
    )


def _jobs(count: int, start: int = 0) -> list[dict]:
    return [
        {"job_id": f"cat-{i}", "job_url": f"https://cat.example/jobs/{i}",
         "company": "Caterpillar", "title": f"Engineer {i}", "location": "Peoria, IL"}
        for i in range(start, start + count)
    ]


def _seeded_db(tmp_path, stored: int = 138) -> tuple[JobDatabase, list[dict]]:
    database = JobDatabase(tmp_path / "jobs.db")
    rows = _jobs(stored)
    database.upsert_jobs(rows)
    return database, rows


def test_a_collapsed_harvest_withholds_removal_sync(tmp_path):
    database, _ = _seeded_db(tmp_path)
    with database:
        assert len(database.company_ids("Caterpillar")) == 138

        result = CompanyResult(
            company="Caterpillar", jobs=_jobs(4), plan=_plan(),
            success=True, complete=True,  # the collector believes it finished
        )
        stats = sync_completed_companies(
            [result], database, previous_counts={"Caterpillar": 138},
        )

        assert stats["skipped_collapsed"] == 1
        assert stats["synced"] == 0
        assert stats["removed"] == 0
        assert stats["collapsed"] == [("Caterpillar", 4, 138)]
        assert len(database.company_ids("Caterpillar")) == 138


def test_a_collapsed_harvest_still_upserts_what_it_found(tmp_path):
    """Withholding removal must not also discard the rows we did collect."""
    database, _ = _seeded_db(tmp_path)
    with database:
        fresh = _jobs(4, start=500)
        result = CompanyResult(
            company="Caterpillar", jobs=fresh, plan=_plan(),
            success=True, complete=True,
        )
        sync_completed_companies(
            [result], database, previous_counts={"Caterpillar": 138},
        )
        ids = database.company_ids("Caterpillar")
        assert len(ids) == 142, "the four new rows are stored alongside the old"
        assert "cat-500" in ids


def test_the_guard_clears_on_the_next_run(tmp_path):
    """A real, sustained halving must not be blocked forever.

    The comparison is against the previous *run*, so once a low count has been
    reported once, the run after it sees no collapse and the removals proceed.
    """
    database, _ = _seeded_db(tmp_path)
    with database:
        result = CompanyResult(
            company="Caterpillar", jobs=_jobs(4), plan=_plan(),
            success=True, complete=True,
        )
        # Second run in a row at 4: no collapse relative to the previous run.
        stats = sync_completed_companies(
            [result], database, previous_counts={"Caterpillar": 4},
        )
        assert stats["skipped_collapsed"] == 0
        assert stats["synced"] == 1


def test_a_healthy_harvest_syncs_normally(tmp_path):
    database, rows = _seeded_db(tmp_path)
    with database:
        result = CompanyResult(
            company="Caterpillar", jobs=rows, plan=_plan(),
            success=True, complete=True,
        )
        stats = sync_completed_companies(
            [result], database, previous_counts={"Caterpillar": 138},
        )
        assert stats["synced"] == 1
        assert stats["skipped_collapsed"] == 0


def test_no_previous_counts_means_no_guard(tmp_path):
    """A first run must not be paralysed by having nothing to compare to."""
    database, _ = _seeded_db(tmp_path)
    with database:
        result = CompanyResult(
            company="Caterpillar", jobs=_jobs(4), plan=_plan(),
            success=True, complete=True,
        )
        stats = sync_completed_companies([result], database, previous_counts=None)
        assert stats["skipped_collapsed"] == 0
        assert stats["synced"] == 1


def test_incompleteness_is_checked_before_collapse(tmp_path):
    """An already-incomplete scrape is reported as such, not as a collapse.

    Both withhold removal sync, so the outcome is the same either way - but the
    two mean different things to a reader of the summary, and a truncated walk
    should not be relabelled.
    """
    database, _ = _seeded_db(tmp_path)
    with database:
        result = CompanyResult(
            company="Caterpillar", jobs=_jobs(4), plan=_plan(),
            success=True, complete=False, stop_reason="budget_exhausted",
        )
        stats = sync_completed_companies(
            [result], database, previous_counts={"Caterpillar": 138},
        )
        assert stats["skipped_incomplete"] == 1
        assert stats["skipped_collapsed"] == 0
        assert stats["removed"] == 0
