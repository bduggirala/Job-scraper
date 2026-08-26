"""What "a short page" is short *of*.

``paginate`` ended a walk when a page came back smaller than the size it asked
for. That reads as "the provider ran out", and usually it is - but the page size
a collector passes is only an assumption about the provider, and for the five
collectors that report no total it is the *only* thing standing between a real
job list and a silently truncated one.

The assumption is measurably wrong. Probing six live iCIMS tenants against the
collector's hard-coded ``ROWS_PER_PAGE = 20``:

    careers-healthequity.icims.com    20 rows/page
    careers-realpagepms.icims.com     50
    career-celanese.icims.com         20
    career-schwab.icims.com           50
    careers-judge.icims.com           13
    careers-aerotek.icims.com         21

Four of the six serve something other than 20. A tenant serving fewer than the
assumed size on every page - which nothing prevents - would have ended the walk
after page one and been reported as a *complete* scrape, which is the one error
that lets removal sync delete live postings.

``ats/taleo.py`` already worked around this by hand, with the same reasoning and
a real portal behind it ("a portal serving 15 a page stopped after page one and
reported success"). Putting it in the shared walk retires that workaround and
covers the other eleven collectors at the same time.

The cost is one extra request, only for a company whose *first* page is short.
"""

from ats.base import STOP_EXHAUSTED
from ats.pagination import PageRequest, paginate


def _rows(start, count, total):
    end = min(start + count, total)
    return [{"id": i} for i in range(start, end)]


def test_a_provider_serving_fewer_rows_than_requested_is_walked_to_the_end():
    """The defect: 15 < 20 ended the walk with 15 of 95 jobs, marked complete."""
    served = 15
    calls = {"n": 0}

    def fetch(request: PageRequest):
        calls["n"] += 1
        return _rows(request.page_index * served, served, total=95), None

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert len(result.items) == 95, (
        f"stopped after {calls['n']} page(s) with {len(result.items)} of 95 rows"
    )
    assert result.complete is True


def test_the_end_is_judged_against_what_the_provider_actually_served():
    """Once page one shows the real size, a genuinely short page still ends it."""
    def fetch(request: PageRequest):
        # 15 a page until the last, which carries 6.
        return _rows(request.page_index * 15, 15, total=51), None

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert len(result.items) == 51
    assert result.stop_reason == STOP_EXHAUSTED
    assert result.pages_fetched == 4, "walked more pages than the provider had"


def test_a_full_first_page_keeps_the_requested_size_as_the_yardstick():
    """No behaviour change for the ordinary case: page one fills, a later page
    falls short, and that short page ends the walk with no extra request."""
    calls = {"n": 0}

    def fetch(request: PageRequest):
        calls["n"] += 1
        return _rows(request.offset, 20, total=27), None

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert len(result.items) == 27
    assert calls["n"] == 2, "spent a request confirming an unambiguous ending"


def test_confirming_a_short_first_page_costs_exactly_one_extra_request():
    """The whole price of the guard, and it must not grow."""
    calls = {"n": 0}

    def fetch(request: PageRequest):
        calls["n"] += 1
        return _rows(request.page_index * 13, 13, total=13), None

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert len(result.items) == 13
    assert result.complete is True
    assert calls["n"] == 2, (
        "a 13-row provider should cost one confirming request, not a walk"
    )


def test_an_empty_first_page_does_not_set_the_yardstick_to_zero():
    """Zero rows on page one ends the walk; it must not become the page size,
    because ``len(rows) < 0`` is never true and the walk would never stop."""
    def fetch(request: PageRequest):
        return [], None

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert result.items == []
    assert result.complete is True
