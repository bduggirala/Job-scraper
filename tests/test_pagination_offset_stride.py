"""The offset a walk asks for next has to follow what it was actually given.

``paginate`` computed ``offset = page_index * page_size`` - the offset it would
have reached *if* the provider honoured the page size it asked for. When a
provider caps below that, the stride steps straight over the rows in between.

Found in a full-workbook run, not by reading: Liberty Mutual reported 222
Eightfold postings and the collector returned 50; Fluor reported 679 and
returned 140. Probing the API directly explains both exactly - it honours
``start`` and ignores ``num``, serving ten rows whatever is asked:

    GET libertymutual.eightfold.ai/api/apply/v2/jobs?domain=...&start=0&num=50
        -> count=222, positions=10
    ...&start=50&num=50   -> count=222, positions=10   (rows 50-59)
    ...&start=0&num=100   -> count=222, positions=10

Requesting start=0, 50, 100, 150, 200 therefore collected rows 0-9, 50-59,
100-109, 150-159 and 200-209: ten of every fifty, and 172 of Liberty Mutual's
222 postings were never fetched at all. 679/50 = 14 pages x 10 = 140 for Fluor.

Total reconciliation caught the shortfall and marked both incomplete - which is
why removal sync did not delete the missing rows - but the rows were still lost.

Stepping by rows actually received cannot skip. Where it is wrong it re-fetches
a row instead of missing one, and ``key`` de-duplication absorbs that; the two
errors are not symmetric.
"""

from ats.base import STOP_TOTAL_REACHED
from ats.pagination import PageRequest, paginate


def _rows(start, count, total):
    return [{"id": i} for i in range(start, min(start + count, total))]


def test_a_provider_that_caps_below_the_requested_page_size_loses_no_rows():
    """The Eightfold shape: honours start, ignores num, always serves ten."""
    served = 10
    seen_offsets: list[int] = []

    def fetch(request: PageRequest):
        seen_offsets.append(request.offset)
        return _rows(request.offset, served, total=222), 222

    result = paginate(fetch, page_size=50, max_jobs=10_000,
                      key=lambda row: row["id"])

    ids = sorted(row["id"] for row in result.items)
    assert ids == list(range(222)), (
        f"collected {len(ids)} of 222 rows; offsets requested: {seen_offsets[:8]}"
    )
    assert result.complete is True
    assert result.stop_reason == STOP_TOTAL_REACHED


def test_the_offset_steps_by_rows_received_not_by_the_size_requested():
    seen_offsets: list[int] = []

    def fetch(request: PageRequest):
        seen_offsets.append(request.offset)
        return _rows(request.offset, 10, total=100), 100

    paginate(fetch, page_size=50, max_jobs=10_000)

    assert seen_offsets[:5] == [0, 10, 20, 30, 40], (
        f"stride ignored what the provider served: {seen_offsets[:5]}"
    )


def test_a_provider_honouring_its_page_size_is_unaffected():
    """The ordinary case must not change: offset stays page_index * page_size."""
    seen_offsets: list[int] = []

    def fetch(request: PageRequest):
        seen_offsets.append(request.offset)
        return _rows(request.offset, 20, total=95), 95

    result = paginate(fetch, page_size=20, max_jobs=10_000)

    assert seen_offsets == [0, 20, 40, 60, 80]
    assert len(result.items) == 95


def test_the_page_counters_still_count_pages_not_rows():
    """``page_index``/``page_number`` address pages; only ``offset`` is a row
    cursor, and a capped provider must not renumber the pages."""
    seen: list[tuple[int, int, int]] = []

    def fetch(request: PageRequest):
        seen.append((request.offset, request.page_index, request.page_number))
        return _rows(request.offset, 10, total=40), 40

    paginate(fetch, page_size=50, max_jobs=10_000)

    assert seen == [(0, 0, 1), (10, 1, 2), (20, 2, 3), (30, 3, 4)]


def test_rows_dropped_as_duplicates_do_not_rewind_the_cursor():
    """The cursor tracks what the provider handed over, not what survived
    de-duplication - otherwise an overlapping page would make the walk ask for
    rows it already has, forever."""
    calls = {"n": 0}

    def fetch(request: PageRequest):
        calls["n"] += 1
        # Every page repeats its first five rows from the previous page.
        start = max(0, request.offset - 5)
        return _rows(start, 20, total=200), None

    result = paginate(fetch, page_size=20, max_jobs=10_000,
                      key=lambda row: row["id"])

    ids = [row["id"] for row in result.items]
    assert len(ids) == len(set(ids)), "duplicates survived"
    assert calls["n"] < 30, f"the cursor stalled: {calls['n']} requests"
