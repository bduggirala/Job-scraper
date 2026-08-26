"""Scrolling a virtualized list must not be read as "no more results".

The lazy-load path decided whether a scroll had achieved anything by comparing
``document.body.scrollHeight`` before and after, and returned ``exhausted=True``
the moment the height did not grow. That is the wrong signal for the one case
where it matters most: a virtualized list (react-window, ag-grid, and every
enterprise careers grid built on them) keeps its scroll height *fixed on
purpose*, recycling a small pool of DOM rows as you move through thousands of
records. Height never grows, so the walk stopped after a single scroll - and,
worse, reported the result as a complete harvest, which lets removal sync
delete every posting it never reached.

Height is now one signal among two: a scroll that did not grow the page is
inconclusive on its own, and the row-level barren counter - which tracks
whether we are still *collecting jobs* - decides instead.

Driven against a fake page, so no Chromium is needed.
"""

from __future__ import annotations

import pytest

from browser import playwright_scraper as ps


class _VirtualizedPage:
    """Fixed scroll height, new rows on every scroll - the real pattern.

    Exposes no pagination control, so the loop must reach the scroll branch.
    """

    def __init__(self, total_rows=120, rows_per_view=10):
        self.total_rows = total_rows
        self.rows_per_view = rows_per_view
        self.scrolls = 0
        self.height = 1000  # never changes: that is the whole point

    def locator(self, selector):
        return _NoControl()

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        return self.height

    @property
    def mouse(self):
        return self

    def wheel(self, x, y):
        self.scrolls += 1

    def visible_rows(self):
        start = self.scrolls * self.rows_per_view
        end = min(start + self.rows_per_view, self.total_rows)
        return [
            {"job_url": f"https://acme.example/jobs/{i}", "title": f"Data Engineer {i}"}
            for i in range(start, end)
        ]


class _NoControl:
    """No load-more / next button anywhere on the page."""

    def __init__(self):
        self.first = self

    def count(self):
        return 0

    def is_visible(self, timeout=None):
        return False

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        raise AssertionError("there is no control to click")


@pytest.fixture
def virtualized(monkeypatch):
    page = _VirtualizedPage()
    monkeypatch.setattr(ps, "_extract_job_rows", lambda p: p.visible_rows())
    return page


def test_a_virtualized_list_is_walked_past_the_first_screen(virtualized):
    """The bug: one scroll, 10 rows, reported complete - against 120 real rows."""
    rows, exhausted = ps._paginate_and_extract(
        virtualized, virtualized.visible_rows(), max_clicks=40, timeout_ms=1000,
    )

    assert len(rows) == 120, "every recycled row must be collected, not just screen one"
    assert exhausted is True, "it really did reach the end"


def test_it_still_stops_once_the_rows_stop_changing(virtualized):
    """Not an infinite loop: a fixed height plus no new rows is the end."""
    rows, exhausted = ps._paginate_and_extract(
        virtualized, virtualized.visible_rows(), max_clicks=200, timeout_ms=1000,
    )

    assert exhausted is True
    # 12 scrolls to exhaust 120 rows, then the barren scrolls that prove it.
    assert virtualized.scrolls <= 20, f"stopped promptly ({virtualized.scrolls} scrolls)"


def test_a_genuinely_static_page_ends_immediately():
    """One scroll, no height growth, no new rows - do not keep scrolling."""

    class _Static(_VirtualizedPage):
        def visible_rows(self):
            return [{"job_url": "https://acme.example/jobs/1", "title": "Data Engineer"}]

    page = _Static()
    import browser.playwright_scraper as mod
    original = mod._extract_job_rows
    mod._extract_job_rows = lambda p: p.visible_rows()
    try:
        rows, exhausted = ps._paginate_and_extract(
            page, page.visible_rows(), max_clicks=40, timeout_ms=1000,
        )
    finally:
        mod._extract_job_rows = original

    assert exhausted is True
    assert len(rows) == 1
    assert page.scrolls == 1, "a static page costs exactly one probing scroll"


def test_the_budget_still_bounds_a_virtualized_walk(virtualized, monkeypatch):
    """A huge virtualized grid must not be able to eat the company timeout."""
    ticks = iter([0.0] + [100.0] * 500)
    monkeypatch.setattr(ps.time, "monotonic", lambda: next(ticks))

    rows, exhausted = ps._paginate_and_extract(
        virtualized, virtualized.visible_rows(), max_clicks=200, timeout_ms=1000,
        budget_seconds=10.0,
    )

    assert exhausted is False, "cut short by the budget, so not a complete harvest"


def test_scroll_growth_alone_still_drives_an_ordinary_infinite_scroll(monkeypatch):
    """The classic case must keep working: height grows, rows append."""

    class _Growing(_VirtualizedPage):
        def wheel(self, x, y):
            self.scrolls += 1
            self.height += 1000

        def visible_rows(self):
            end = min((self.scrolls + 1) * self.rows_per_view, self.total_rows)
            return [
                {"job_url": f"https://acme.example/jobs/{i}", "title": f"Data Engineer {i}"}
                for i in range(0, end)
            ]

    page = _Growing()
    monkeypatch.setattr(ps, "_extract_job_rows", lambda p: p.visible_rows())
    rows, exhausted = ps._paginate_and_extract(
        page, page.visible_rows(), max_clicks=40, timeout_ms=1000,
    )

    assert len(rows) == 120
    assert exhausted is True
