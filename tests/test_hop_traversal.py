import pytest

from browser.playwright_scraper import _hop_key, _navigate_to_job_list

LANDING = """
<html><body>
  <a href="/life">Life at Example</a>
  <a href="/benefits">Benefits</a>
  <a href="/careers/jobs">Jobs</a>
</body></html>
"""

MIDDLE = """
<html><body>
  <a href="/careers/search-jobs">Search Jobs</a>
</body></html>
"""

JOBS = """
<html><body>
  <div><a href="/jobs/1">Senior Data Engineer</a>
       <span class="location">Dallas, TX</span></div>
  <div><a href="/jobs/2">Data Platform Engineer</a>
       <span class="location">Plano, TX</span></div>
</body></html>
"""

PAGES = {
    "https://example.test/": LANDING,
    "https://example.test/life": "<html><body>culture</body></html>",
    "https://example.test/benefits": "<html><body>benefits</body></html>",
    "https://example.test/careers/jobs": MIDDLE,
    "https://example.test/careers/search-jobs": JOBS,
}


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context()

    def handler(route):
        url = route.request.url.split("?")[0]
        body = PAGES.get(url) or PAGES.get(url.rstrip("/") + "/")
        if body is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=body)

    ctx.route("**/*", handler)
    p = ctx.new_page()
    p.goto("https://example.test/")
    yield p
    browser.close()
    pw.stop()


def test_hop_key_ignores_trailing_slash_and_case():
    assert _hop_key("https://E.com/Jobs/") == _hop_key("https://e.com/Jobs")


def test_finds_jobs_two_layers_deep(page):
    result = _navigate_to_job_list("Example", page, timeout_ms=5000)
    titles = {j["title"] for j in result.jobs}
    assert "Senior Data Engineer" in titles
    assert "Data Platform Engineer" in titles
