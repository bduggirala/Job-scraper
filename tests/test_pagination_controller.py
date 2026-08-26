"""The shared pagination controller.

Fourteen collectors independently implement the same walk, which is why the
same defect appeared in eight of them at once. This centralises the loop and
adds the two things none of the hand-written versions had:

* **per-page retry** - a transient failure on page 12 previously ended
  pagination and marked the scrape incomplete, suppressing removal sync for
  that company. Most such failures recover on a second attempt.
* **repeated-page detection** - a provider that ignores its own paging
  parameter serves page 1 forever; walking it to the budget wastes hundreds of
  requests to collect nothing new.
"""

import pytest

from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_NO_NEW_ROWS,
    STOP_PAGE_FAILED,
    STOP_REPEATED_PAGE,
    STOP_TOTAL_REACHED,
)
from ats.pagination import PageRequest, paginate


def _rows(start, count, total=None):
    end = start + count if total is None else min(start + count, total)
    return [{"id": i} for i in range(start, end)]


def test_an_offset_walk_collects_every_page():
    def fetch(req: PageRequest):
        return _rows(req.offset, 20, total=95), 95

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert len(result.items) == 95
    assert result.complete is True
    assert result.stop_reason == STOP_TOTAL_REACHED
    assert result.pages_fetched == 5


def test_a_walk_with_no_reported_total_ends_on_an_empty_page():
    def fetch(req: PageRequest):
        return _rows(req.offset, 20, total=50), None

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert len(result.items) == 50
    assert result.complete is True
    assert result.stop_reason == STOP_EXHAUSTED


def test_a_transient_page_failure_is_retried_and_the_walk_completes():
    """The headline improvement: one 503 no longer truncates a company."""
    attempts = {"n": 0}

    def fetch(req: PageRequest):
        if req.offset == 40:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("HTTP 503")
        return _rows(req.offset, 20, total=100), 100

    result = paginate(fetch, page_size=20, max_jobs=10_000, page_retries=2)

    assert result.complete is True, "a retryable page still truncated the walk"
    assert len(result.items) == 100
    assert attempts["n"] == 2


def test_a_page_failing_every_attempt_marks_the_walk_incomplete():
    def fetch(req: PageRequest):
        if req.offset == 40:
            raise RuntimeError("HTTP 503")
        return _rows(req.offset, 20, total=200), 200

    result = paginate(fetch, page_size=20, max_jobs=10_000, page_retries=2)

    assert result.complete is False
    assert result.stop_reason == STOP_PAGE_FAILED
    assert len(result.items) == 40


def test_a_first_page_failure_propagates_so_the_collector_can_fall_back():
    def fetch(req: PageRequest):
        raise RuntimeError("HTTP 404")

    with pytest.raises(RuntimeError):
        paginate(fetch, page_size=20, max_jobs=10_000, page_retries=1)


def test_the_job_budget_marks_the_walk_incomplete():
    def fetch(req: PageRequest):
        return _rows(req.offset, 20), 9000

    result = paginate(fetch, page_size=20, max_jobs=100)

    assert result.complete is False
    assert result.stop_reason == STOP_BUDGET
    assert len(result.items) == 100


def test_a_provider_serving_the_same_page_forever_is_detected():
    """A tenant ignoring its paging parameter must not burn the whole budget."""
    calls = {"n": 0}

    def fetch(req: PageRequest):
        calls["n"] += 1
        return _rows(0, 20), 9000          # always page 1

    result = paginate(fetch, page_size=20, max_jobs=10_000,
                      key=lambda row: row["id"])

    assert result.stop_reason in {STOP_REPEATED_PAGE, STOP_NO_NEW_ROWS}
    assert calls["n"] < 10, f"kept requesting a repeated page {calls['n']} times"
    assert len(result.items) == 20


def test_duplicate_rows_across_pages_are_collapsed_by_key():
    def fetch(req: PageRequest):
        # Pages overlap by 5 rows.
        start = max(0, req.offset - 5 * req.page_index)
        return _rows(start, 20, total=60), None

    result = paginate(fetch, page_size=20, max_jobs=10_000,
                      key=lambda row: row["id"])

    ids = [r["id"] for r in result.items]
    assert len(ids) == len(set(ids)), "duplicates survived the walk"


def test_a_short_page_ends_the_walk_when_no_total_is_reported():
    def fetch(req: PageRequest):
        return (_rows(req.offset, 20) if req.offset == 0 else _rows(req.offset, 7)), None

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert result.complete is True
    assert len(result.items) == 27


def test_the_request_carries_both_offset_and_page_number():
    """Providers index differently - offset, 0-based page, 1-based page."""
    seen: list[tuple[int, int, int]] = []

    def fetch(req: PageRequest):
        seen.append((req.offset, req.page_index, req.page_number))
        return _rows(req.offset, 10, total=30), 30

    paginate(fetch, page_size=10, max_jobs=10_000)

    assert seen == [(0, 0, 1), (10, 1, 2), (20, 2, 3)]


def test_a_slightly_over_reported_total_does_not_prevent_completion():
    """Totals drift while a walk runs - postings close, counts are cached.

    Treating a few missing rows as a failure would suppress that company's
    removal sync on every run forever, so a small gap is tolerated (see
    ``ats.base.TOTAL_RECONCILIATION_TOLERANCE``).
    """
    def fetch(req: PageRequest):
        return _rows(req.offset, 20, total=98), 100

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert result.complete is True
    assert len(result.items) == 98


def test_a_large_shortfall_against_a_reported_total_is_not_completion():
    """Deliberate policy, replacing an earlier rule that called this complete.

    30 rows served against a reported 900 is ambiguous: either the tenant is
    over-reporting a total for a different scope, or it stalled and 870 live
    postings exist that we never saw. Nothing in a single walk distinguishes
    them, so the choice is which error to make - and they are not symmetric.

    Believing the tenant costs 870 live postings deleted from the tracker on
    the next sync, plus the false "new" burst when they reappear. Disbelieving
    it costs a suppressed removal sync, which leaves stale rows behind and is
    visible in the run summary's truncation table. The recoverable error wins.
    """
    def fetch(req: PageRequest):
        return _rows(req.offset, 20, total=30), 900

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert result.complete is False
    assert len(result.items) == 30, "the rows we did get are still kept"
    assert result.reported_total == 900
