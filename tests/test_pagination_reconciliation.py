"""A walk that stopped short of what the provider reported is not complete.

``complete`` gates removal sync, so claiming it wrongly is how live postings
get deleted. Three ways the walk could stop short and still report success:

* **the page ceiling.** ``MAX_PAGES`` bounds the loop independently of the job
  budget, but the stop reason was derived only from the job budget - so a walk
  that ran out of *pages* reported ``exhausted``. On a provider serving ten
  rows per request that ceiling is 5,000 jobs, and CVS Health lists 19,246.
  The company cited in ``notify.should_send``'s own docstring as the reason
  budget truncation must not silence the digest never reached that branch,
  because it was being reported complete.

* **a short or empty page while the provider's own total says otherwise.**
  A tenant that stops serving rows at 200 of a reported 5,000 is not done; it
  has failed silently. Believing it deletes 4,800 live postings.

* **a repeated page.** Stopping on the repeat is right - continuing would loop
  forever - but what was collected is still only part of the list.

Where no total is reported there is nothing to reconcile against, and an
honest short page still means exhausted.
"""


from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_PAGE_CEILING,
    STOP_REPEATED_PAGE,
    STOP_SHORT_OF_TOTAL,
)
from ats.pagination import MAX_PAGES, paginate


def _serving(total_available, reported=None, page_size=10):
    """A provider with ``total_available`` rows that reports ``reported``."""
    def fetch(request):
        start = request.offset
        rows = [{"id": start + i} for i in range(request.page_size)
                if start + i < total_available]
        return rows, reported
    return fetch


def test_running_out_of_pages_is_not_the_same_as_running_out_of_jobs():
    """CVS shape: 10 rows per request against a 19,246-posting employer."""
    walk = paginate(_serving(19_246, reported=19_246), page_size=10,
                    max_jobs=8_000, label="cvs")

    assert len(walk.items) == MAX_PAGES * 10
    assert walk.complete is False, (
        f"collected {len(walk.items)} of 19,246 and called it complete "
        f"(stop_reason={walk.stop_reason!r})"
    )
    assert walk.stop_reason == STOP_PAGE_CEILING


def test_the_job_budget_still_reports_itself_as_the_budget():
    """The digest rule keys on this reason specifically - it must not drift."""
    walk = paginate(_serving(50_000, reported=50_000, page_size=500),
                    page_size=500, max_jobs=8_000, label="big")

    assert walk.complete is False
    assert walk.stop_reason == STOP_BUDGET


def test_an_empty_page_short_of_a_reported_total_is_incomplete():
    walk = paginate(_serving(200, reported=5_000, page_size=100),
                    page_size=100, max_jobs=10_000, label="stalled")

    assert len(walk.items) == 200
    assert walk.complete is False, "believed a provider that stopped early"
    assert walk.stop_reason == STOP_SHORT_OF_TOTAL
    assert walk.reported_total == 5_000


def test_a_repeated_page_short_of_a_reported_total_is_incomplete():
    def repeating(request):
        return [{"id": i} for i in range(100)], 5_000

    walk = paginate(repeating, page_size=100, max_jobs=10_000, label="repeat")

    assert len(walk.items) == 100
    assert walk.complete is False
    # The reason still names the observed event: "repeated_page" is what
    # actually happened and is what an operator needs in order to diagnose it.
    # Only "exhausted" - a claim of completeness the total contradicts - is
    # rewritten.
    assert walk.stop_reason == STOP_REPEATED_PAGE


def test_reaching_the_reported_total_is_complete():
    walk = paginate(_serving(350, reported=350, page_size=100),
                    page_size=100, max_jobs=10_000, label="whole")

    assert len(walk.items) == 350
    assert walk.complete is True


def test_a_provider_that_over_reports_slightly_is_still_complete():
    """Reported totals drift as postings close mid-walk. A tenant that says
    350 and serves 348 has not failed, and marking it incomplete forever would
    suppress its removal sync permanently."""
    walk = paginate(_serving(348, reported=350, page_size=100),
                    page_size=100, max_jobs=10_000, label="drift")

    assert len(walk.items) == 348
    assert walk.complete is True, "a 2-row drift was treated as a failure"


def test_no_reported_total_means_an_honest_short_page_still_ends_the_walk():
    walk = paginate(_serving(35, reported=None, page_size=20),
                    page_size=20, max_jobs=10_000, label="honest")

    assert len(walk.items) == 35
    assert walk.complete is True
    assert walk.stop_reason == STOP_EXHAUSTED


def test_a_repeated_page_with_no_reported_total_is_still_complete():
    """Nothing contradicts the repeat, so stopping on it is the whole list."""
    calls = {"n": 0}

    def repeating(request):
        calls["n"] += 1
        return [{"id": i} for i in range(5)], None

    walk = paginate(repeating, page_size=5, max_jobs=10_000, label="norepeat")
    assert walk.complete is True
    assert walk.stop_reason == STOP_REPEATED_PAGE
