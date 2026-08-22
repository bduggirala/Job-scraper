"""Tests for _paginate_and_extract - the pagination accumulator.

Regression coverage for a real bug found live: Goldman Sachs' career site
paginates with a genuine "next page" link (matched by the same
LOAD_MORE_SELECTORS entry used for "Load more" buttons), which *replaces*
the page's content rather than appending to it. The old code extracted rows
exactly once, after all clicking finished, so it silently kept only the
final page and discarded every page before it - confirmed directly: after
one click, only 1 of the original 20 extracted jobs was still present.

Uses a real headless Chromium page (like test_hop_traversal.py) so
_extract_job_rows' actual page.evaluate() JS runs against real DOM/JS
behavior, not a mock standing in for it.
"""

import pytest

from browser.playwright_scraper import _paginate_and_extract, _extract_job_rows

# An accumulating "Load more" button: each click appends a new job link to
# the same document without removing the old ones.
LOAD_MORE_PAGE = """
<html><body>
  <div id="jobs">
    <a href="/jobs/1">Data Engineer Alpha</a>
  </div>
  <button onclick="
    var d = document.createElement('a');
    var n = document.querySelectorAll('#jobs a').length + 1;
    d.href = '/jobs/' + n;
    d.textContent = 'Data Engineer Role ' + n;
    document.getElementById('jobs').appendChild(d);
  ">Load more</button>
</body></html>
"""

# A genuine "next page" link: navigates to an entirely different document
# whose job list has no overlap with the current one - the Goldman Sachs
# pattern (a[aria-label*="next" i]).
NEXT_PAGE_1 = """
<html><body>
  <a href="/jobs/1">Data Engineer Page One A</a>
  <a href="/jobs/2">Data Engineer Page One B</a>
  <a href="/results?page=2" aria-label="Next page">Next</a>
</body></html>
"""
NEXT_PAGE_2 = """
<html><body>
  <a href="/jobs/3">Data Engineer Page Two A</a>
  <a href="/jobs/4">Data Engineer Page Two B</a>
</body></html>
"""


@pytest.fixture
def browser_ctx():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    yield ctx
    browser.close()
    pw.stop()


def _page_with(ctx, routes: dict[str, str], start_url: str):
    def handler(route):
        url = route.request.url.split("?")[0]
        key = route.request.url if route.request.url in routes else url
        body = routes.get(key)
        if body is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=body)

    ctx.route("**/*", handler)
    page = ctx.new_page()
    page.goto(start_url)
    return page


def test_accumulates_across_an_appending_load_more_button(browser_ctx):
    page = _page_with(browser_ctx, {"https://example.test/": LOAD_MORE_PAGE}, "https://example.test/")
    initial = _extract_job_rows(page)
    assert len(initial) == 1

    rows = _paginate_and_extract(page, initial, max_clicks=3, timeout_ms=5000)

    # One click appends 1 new job each time -> 1 initial + 3 clicks = 4 total.
    assert len(rows) == 4
    titles = {r["title"] for r in rows}
    assert "Data Engineer Alpha" in titles
    assert len(titles) == 4  # no duplicates, nothing lost


def test_accumulates_across_a_content_replacing_next_link(browser_ctx):
    """The exact Goldman Sachs bug: a "next" link replaces the page's
    content instead of appending - both pages' jobs must survive."""
    routes = {
        "https://example.test/results": NEXT_PAGE_1,
        "https://example.test/results?page=2": NEXT_PAGE_2,
    }
    page = _page_with(browser_ctx, routes, "https://example.test/results")
    initial = _extract_job_rows(page)
    assert len(initial) == 2  # page one only, before any pagination

    rows = _paginate_and_extract(page, initial, max_clicks=1, timeout_ms=5000)

    # Old behavior would return only page two's 2 jobs (content replaced).
    # Fixed behavior accumulates both pages: 4 total.
    assert len(rows) == 4
    urls = {r["job_url"] for r in rows}
    assert any(u.endswith("/jobs/1") for u in urls)
    assert any(u.endswith("/jobs/2") for u in urls)
    assert any(u.endswith("/jobs/3") for u in urls)
    assert any(u.endswith("/jobs/4") for u in urls)


def test_stops_when_nothing_new_appears(browser_ctx):
    """No pagination control and no scroll growth -> returns just the
    initial rows without hanging or erroring."""
    page = _page_with(
        browser_ctx,
        {"https://example.test/": "<html><body><a href='/jobs/1'>Data Engineer Solo</a></body></html>"},
        "https://example.test/",
    )
    initial = _extract_job_rows(page)
    rows = _paginate_and_extract(page, initial, max_clicks=5, timeout_ms=5000)
    assert len(rows) == 1
