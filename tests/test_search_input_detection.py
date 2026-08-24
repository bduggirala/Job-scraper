"""Regression coverage for _find_search_input's location/search overlap.

Real bug found live: Pyramid Consulting's job board (jobs.sprockets.ai) has
exactly one input, placeholder "Search by city, zip, or role" - a combined
location+keyword box, the only search UI the site has. The location-hint
check ran unconditionally before the search-hint check, so a field matching
both ("city", "zip" AND "search", "role") was always rejected as
location-only, leaving the site with no usable search input and 0 jobs
returned even though the site has hundreds of open postings.
"""

import pytest

from browser.playwright_scraper import _find_search_input

COMBINED_FIELD_PAGE = """
<html><body>
  <input type="text" placeholder="Search by city, zip, or role">
</body></html>
"""

PURE_LOCATION_PAGE = """
<html><body>
  <input type="text" placeholder="City, state, or zip code">
</body></html>
"""

# Mphasis's search field placeholder is "Title | Skill" - bare "title" with
# no "job" prefix. The hint regex only matched the compound "job.?title",
# so this field (Mphasis's only keyword input) was never found at all.
BARE_TITLE_FIELD_PAGE = """
<html><body>
  <input type="text" placeholder="Title | Skill">
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


def test_accepts_a_combined_location_and_keyword_field(browser_ctx):
    page = browser_ctx.new_page()
    page.set_content(COMBINED_FIELD_PAGE)
    found = _find_search_input(page)
    assert found is not None


def test_still_rejects_a_pure_location_field(browser_ctx):
    page = browser_ctx.new_page()
    page.set_content(PURE_LOCATION_PAGE)
    found = _find_search_input(page)
    assert found is None


def test_accepts_a_bare_title_field(browser_ctx):
    page = browser_ctx.new_page()
    page.set_content(BARE_TITLE_FIELD_PAGE)
    found = _find_search_input(page)
    assert found is not None
