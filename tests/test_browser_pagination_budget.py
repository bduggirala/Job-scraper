"""The browser's pagination click loop is bounded by time as well as count.

``playwright.max_pages`` alone was the wrong bound for a large employer. Raising
it far enough to finish IBM, CBRE or Goldman Sachs would also let one slow site
spend the entire ``browser_company_timeout_seconds`` inside the click loop, and
overshooting that limit is recorded as a Timeout *failure* - which discards
every row already collected, losing the company outright. That is strictly
worse than the truncation the higher count was meant to fix.

With both bounds the count can be generous: whichever trips first ends the walk,
and either way it returns ``exhausted=False`` so the caller marks the company
truncated (removal sync skipped, listed in the run summary) rather than silently
complete.

These tests drive :func:`browser.playwright_scraper._paginate_and_extract`
against a fake page, so they need no Chromium.
"""

from __future__ import annotations

import pytest

from browser import playwright_scraper as ps


class _FakePage:
    """A page whose "next" button always yields one more never-seen row."""

    def __init__(self, rows_per_click: int = 5):
        self.rows_per_click = rows_per_click
        self.clicks = 0
        self._height = 1000

    # -- the bits _paginate_and_extract touches -----------------------------
    def locator(self, selector):
        return _FakeLocator(self)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        return self._height

    @property
    def mouse(self):
        return self

    def wheel(self, x, y):
        self._height += 1000


class _FakeLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    def count(self):
        return 1

    def is_visible(self, timeout=None):
        return True

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        self.page.clicks += 1


def _rows_for(page) -> list[dict]:
    """Every click reveals a fresh batch, so the loop never goes barren."""
    start = page.clicks * page.rows_per_click
    return [
        {"job_url": f"https://acme.example/jobs/{i}", "title": f"Engineer {i}"}
        for i in range(start, start + page.rows_per_click)
    ]


@pytest.fixture
def endless_page(monkeypatch):
    page = _FakePage()
    monkeypatch.setattr(ps, "_extract_job_rows", lambda p: _rows_for(p))
    return page


def test_the_click_count_still_bounds_the_walk(endless_page):
    rows, exhausted = ps._paginate_and_extract(
        endless_page, _rows_for(endless_page), max_clicks=4, timeout_ms=1000,
    )
    assert endless_page.clicks == 4
    assert exhausted is False, "a walk cut short must not claim to be exhausted"
    assert len(rows) == 25  # the initial batch plus four clicks


def test_a_zero_budget_means_count_only(endless_page):
    """Callers that pass no budget keep the old behaviour exactly."""
    rows, exhausted = ps._paginate_and_extract(
        endless_page, _rows_for(endless_page), max_clicks=3, timeout_ms=1000,
        budget_seconds=0.0,
    )
    assert endless_page.clicks == 3
    assert exhausted is False


def test_an_expired_budget_stops_before_the_count_does(endless_page, monkeypatch):
    """A slow paginator is truncated, not left to blow the company timeout."""
    ticks = iter([0.0] + [100.0] * 200)  # first read arms the deadline, then it is past
    monkeypatch.setattr(ps.time, "monotonic", lambda: next(ticks))

    rows, exhausted = ps._paginate_and_extract(
        endless_page, _rows_for(endless_page), max_clicks=40, timeout_ms=1000,
        budget_seconds=10.0,
    )

    assert endless_page.clicks == 0, "the budget was already spent"
    assert exhausted is False, "stopping on the budget is a truncation, not exhaustion"
    assert len(rows) == 5, "the rows already in hand are kept"


def test_a_generous_budget_lets_the_count_run_to_completion(endless_page):
    rows, exhausted = ps._paginate_and_extract(
        endless_page, _rows_for(endless_page), max_clicks=12, timeout_ms=100,
        budget_seconds=600.0,
    )
    assert endless_page.clicks == 12
    assert len(rows) == 65


def test_an_inert_control_ends_the_walk_early(monkeypatch):
    """Two barren clicks stop the loop long before either bound."""
    page = _FakePage()
    monkeypatch.setattr(ps, "_extract_job_rows", lambda p: [
        {"job_url": "https://acme.example/jobs/1", "title": "Engineer 1"},
    ])

    rows, exhausted = ps._paginate_and_extract(
        page, [], max_clicks=40, timeout_ms=100, budget_seconds=600.0,
    )

    # The first click genuinely contributes the row; only the two after it are
    # barren, and it takes both to prove the control is inert.
    assert page.clicks == 3
    assert exhausted is True, "running out of results IS exhaustion"
    assert len(rows) == 1


def test_the_configured_defaults_keep_pagination_inside_the_company_timeout():
    """The two settings must stay consistent, or the fix defeats itself."""
    from settings import load_settings

    cfg = load_settings()
    budget = float(cfg.get("playwright.pagination_budget_seconds", 150))
    company = float(cfg.get("concurrency.browser_company_timeout_seconds", 480))

    assert budget > 0
    assert budget < company, (
        "pagination must give up before the per-company limit does, or a slow "
        "paginator is recorded as a Timeout failure and its rows are discarded"
    )
