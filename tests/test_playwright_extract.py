import pytest

from browser.playwright_scraper import _extract_job_rows

HTML = """
<html><body>
  <div class="card">
    <a href="/jobs/1">Senior Data Engineer</a>
    <span class="job-location">Dallas, TX</span>
    <time datetime="2026-08-18T00:00:00Z">Aug 18</time>
  </div>
  <div class="card">
    <a href="/jobs/2">Analytics Engineer</a>
    <span class="location">Plano, TX</span>
    <span class="posted-date">3 days ago</span>
  </div>
</body></html>
"""


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    p = browser.new_page()
    p.set_content(HTML)
    yield p
    browser.close()
    pw.stop()


def test_extracts_datetime_attribute(page):
    rows = _extract_job_rows(page)
    row = next(r for r in rows if "Senior Data Engineer" in r["title"])
    assert row["date_posted"] == "2026-08-18T00:00:00Z"


def test_extracts_relative_date_text(page):
    rows = _extract_job_rows(page)
    row = next(r for r in rows if "Analytics Engineer" in r["title"])
    assert row["date_posted"] == "3 days ago"


def test_still_extracts_location(page):
    rows = _extract_job_rows(page)
    row = next(r for r in rows if "Senior Data Engineer" in r["title"])
    assert row["location"] == "Dallas, TX"
