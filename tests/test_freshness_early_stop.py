"""On a tenant too big to finish, stop at the freshness window - not at an
arbitrary prefix.

CVS Health lists 19,259 postings. No budget walks that, so the only question is
*which* subset we keep. The Phenom route kept the first 8,000 in relevance
order, which meant jobs posted today sat beyond the cut - measured, offset 0
held a 12 June posting and offset 7,990 one from 24 August. The Workday tenant
its own applyUrls point at serves posting-date descending, and on a descending
feed there is a much better answer: page until the provider stops serving
anything inside the freshness window, then stop. Everything past that point is
older than the window *by construction*, so nothing the filter would have kept
is missed.

Live result: 6,777 jobs in 220s (oldest 12 days) against 8,000 in 432s.

Two guards matter and are tested here. The stop only engages when the walk was
going to be truncated anyway - otherwise it would turn completed scrapes into
partial ones and silently disable their removal sync. And it only fires after
two consecutive fully-stale pages, because real feeds carry ordering noise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from ats.base import (
    DESCRIBABLE_STOP_REASONS,
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_FRESHNESS_REACHED,
    STOP_TOTAL_REACHED,
)
from ats.pagination import paginate

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=9)
PAGE = 20


def _feed(total, *, page_size=PAGE, days_per_row=0.01, undated=False):
    """A newest-first feed: row N is N*days_per_row days old."""
    def fetch(request):
        start = request.offset
        rows = []
        for i in range(start, min(start + page_size, total)):
            age = timedelta(days=i * days_per_row)
            rows.append({
                "job_url": f"https://acme.example/j{i}",
                "date_posted": None if undated else (NOW - age).isoformat(),
            })
        return rows, total
    return fetch


def _walk(total, *, max_jobs, cutoff=CUTOFF, **kw):
    return paginate(
        _feed(total, **kw), page_size=PAGE, max_jobs=max_jobs,
        key=lambda r: r["job_url"], label="test", freshness_cutoff=cutoff,
    )


def test_it_stops_once_the_feed_is_past_the_window():
    # 20,000 rows at 0.01 days each -> row 900 is exactly 9 days old.
    walk = _walk(20000, max_jobs=10000)

    assert walk.stop_reason == STOP_FRESHNESS_REACHED
    assert walk.complete is False
    assert 900 <= len(walk.items) <= 1000, (
        f"stopped at {len(walk.items)}; expected just past the 9-day boundary"
    )


def test_everything_inside_the_window_is_kept():
    """The whole point: no fresh posting may be left behind."""
    walk = _walk(20000, max_jobs=10000)
    kept = {r["job_url"] for r in walk.items}

    for i in range(900):                       # every row younger than 9 days
        assert f"https://acme.example/j{i}" in kept, f"row {i} was inside the window"


def test_it_does_not_engage_when_the_walk_could_finish():
    """A tenant inside the budget must still be collected in full.

    Without this guard, every small Workday tenant would become `partial`,
    which silently disables its removal sync - live postings would then never
    be cleaned up.
    """
    walk = _walk(300, max_jobs=10000)

    assert walk.stop_reason in (STOP_TOTAL_REACHED, STOP_EXHAUSTED)
    assert walk.complete is True
    assert len(walk.items) == 300


def test_one_stale_page_is_not_enough():
    """Real feeds carry ordering noise; a single old page proves nothing."""
    calls = {"n": 0}

    def fetch(request):
        calls["n"] += 1
        # Page 3 is entirely stale, every other page is fresh.
        stale = calls["n"] == 3
        rows = [{
            "job_url": f"https://acme.example/p{calls['n']}r{i}",
            "date_posted": (NOW - timedelta(days=40 if stale else 1)).isoformat(),
        } for i in range(PAGE)]
        return rows, 20000

    walk = paginate(fetch, page_size=PAGE, max_jobs=200,
                    key=lambda r: r["job_url"], label="test",
                    freshness_cutoff=CUTOFF)

    assert walk.stop_reason == STOP_BUDGET, "a lone stale page must not end the walk"
    assert len(walk.items) == 200


def test_undated_rows_do_not_end_the_walk():
    """Treating "no date" as "old" would stop the moment dates went missing."""
    walk = _walk(20000, max_jobs=200, undated=True)

    assert walk.stop_reason == STOP_BUDGET
    assert len(walk.items) == 200


def test_no_cutoff_means_the_old_behaviour_exactly():
    walk = _walk(20000, max_jobs=200, cutoff=None)
    assert walk.stop_reason == STOP_BUDGET
    assert len(walk.items) == 200


def test_the_reason_is_describable():
    """A run carrying only this may still be trusted to say what is new.

    Unlike `budget_exhausted_unordered`, this gap has a known shape: we chose
    where to stop, and we chose the freshness boundary.
    """
    assert STOP_FRESHNESS_REACHED in DESCRIBABLE_STOP_REASONS


def test_a_provider_specific_date_reader_is_used():
    """Workday's walk sees raw jobPostings, not normalized records.

    The date lives in ``postedOn``; reading ``date_posted`` unconditionally
    found no dates at all and the stop silently never fired - which is exactly
    how this shipped broken the first time.
    """
    def fetch(request):
        rows = [{
            "job_url": f"https://acme.example/j{request.offset + i}",
            "postedOn": "Posted 40 Days Ago",
        } for i in range(PAGE)]
        return rows, 20000

    walk = paginate(fetch, page_size=PAGE, max_jobs=5000,
                    key=lambda r: r["job_url"], label="test",
                    freshness_cutoff=CUTOFF,
                    row_date=lambda row: row.get("postedOn"))

    assert walk.stop_reason == STOP_FRESHNESS_REACHED
    assert len(walk.items) == 2 * PAGE, "two stale pages, then stop"
