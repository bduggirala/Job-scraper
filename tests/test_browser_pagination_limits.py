"""The browser paginator must say when it stopped early, and stop stalling.

``_paginate_and_extract`` exited at ``max_clicks`` indistinguishably from
exiting because the site ran out of pages, so a large browser-routed employer
was truncated silently - and that is the biggest routing bucket in the
workbook (89 of 180 companies).

It also had no "nothing new appeared" exit, so a control that stays visible but
does nothing burned every remaining click.
"""

import pytest

from browser.playwright_scraper import _extract_job_rows, _paginate_and_extract


@pytest.fixture
def browser_ctx():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()
    yield ctx
    browser.close()
    pw.stop()


def _page_with(ctx, html: str):
    ctx.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body=html))
    page = ctx.new_page()
    page.goto("https://example.test/")
    return page


# A button that stays visible and clickable but never adds a row.
DEAD_BUTTON = """
<html><body>
  <div id="jobs"><a href="/jobs/1">Data Engineer Alpha</a></div>
  <button onclick="void(0)">Load more</button>
</body></html>
"""

# Appends one row per click, forever - never exhausts.
ENDLESS = """
<html><body>
  <div id="jobs"><a href="/jobs/1">Data Engineer 1</a></div>
  <button onclick="
    var d=document.createElement('a');
    var n=document.querySelectorAll('#jobs a').length+1;
    d.href='/jobs/'+n; d.textContent='Data Engineer '+n;
    document.getElementById('jobs').appendChild(d);
  ">Load more</button>
</body></html>
"""

FINITE = """
<html><body>
  <div id="jobs"><a href="/jobs/1">Data Engineer 1</a></div>
  <button id="b" onclick="
    var n=document.querySelectorAll('#jobs a').length+1;
    if (n<=3){var d=document.createElement('a');d.href='/jobs/'+n;
    d.textContent='Data Engineer '+n;document.getElementById('jobs').appendChild(d);}
    else {document.getElementById('b').remove();}
  ">Load more</button>
</body></html>
"""


def test_a_control_that_adds_nothing_stops_early(browser_ctx):
    """A dead 'Load more' must not burn the whole click budget."""
    page = _page_with(browser_ctx, DEAD_BUTTON)
    rows, exhausted = _paginate_and_extract(
        page, _extract_job_rows(page), max_clicks=10, timeout_ms=5000,
    )

    assert len(rows) == 1
    assert exhausted is True, "a site that ran out should report exhausted"


def test_hitting_the_click_cap_reports_not_exhausted(browser_ctx):
    """The truncation signal: more pages remained when we stopped."""
    page = _page_with(browser_ctx, ENDLESS)
    rows, exhausted = _paginate_and_extract(
        page, _extract_job_rows(page), max_clicks=3, timeout_ms=5000,
    )

    assert len(rows) == 4          # 1 initial + 3 clicks
    assert exhausted is False, "stopped at the cap but claimed to be done"


def test_running_out_of_pages_reports_exhausted(browser_ctx):
    page = _page_with(browser_ctx, FINITE)
    rows, exhausted = _paginate_and_extract(
        page, _extract_job_rows(page), max_clicks=10, timeout_ms=5000,
    )

    assert len(rows) == 3
    assert exhausted is True
