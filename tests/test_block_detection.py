"""A site refusing automated access must be recorded, not retried differently.

A challenge page renders cleanly and contains zero jobs, so it previously took
the "clean render, no jobs" retry path: three full traversals with a rotated
user-agent and viewport each time, up to eight minutes, ending in a generic
``NoJobsFound`` that told nobody what had actually happened.

Detection only. Nothing here solves or bypasses a challenge - the point is to
stop promptly and say so, which is both the honest outcome and the one that
stops wasting the per-company budget.
"""

import pytest

from browser.playwright_scraper import _looks_blocked

CHALLENGE = """
<html><head><title>Attention Required! | Cloudflare</title></head>
<body><h1>Checking your browser before accessing the site</h1>
<p>Please enable JavaScript and cookies to continue</p></body></html>
"""

CAPTCHA = """
<html><head><title>Security check</title></head>
<body><div id="px-captcha">Verify you are human</div></body></html>
"""

DENIED = """
<html><head><title>Access Denied</title></head>
<body><p>Access Denied. You don't have permission to access this resource.</p>
</body></html>
"""

# A perfectly ordinary careers page that happens to use words near the markers.
ORDINARY = """
<html><head><title>Careers at Acme - Security Engineering</title></head>
<body>
  <h1>Open roles</h1>
  <p>We take security seriously. Verification of employment available.</p>
  <a href="/jobs/1">Senior Data Engineer</a>
  <a href="/jobs/2">Data Platform Engineer</a>
</body></html>
"""

EMPTY_RESULTS = """
<html><head><title>Search results</title></head>
<body><p>No jobs match your search. Try a different keyword.</p></body></html>
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


def _page(ctx, html):
    ctx.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body=html))
    page = ctx.new_page()
    page.goto("https://example.test/")
    return page


@pytest.mark.parametrize("html,label", [
    (CHALLENGE, "cloudflare interstitial"),
    (CAPTCHA, "captcha widget"),
    (DENIED, "explicit denial"),
])
def test_a_refusing_page_is_recognised(browser_ctx, html, label):
    assert _looks_blocked(_page(browser_ctx, html)) is True, label


def test_an_ordinary_careers_page_is_not_mistaken_for_a_block(browser_ctx):
    """Matching loosely would strand real companies that merely say 'security'."""
    assert _looks_blocked(_page(browser_ctx, ORDINARY)) is False


def test_an_empty_result_page_is_not_a_block(browser_ctx):
    """'No jobs match' is a real answer, not a refusal."""
    assert _looks_blocked(_page(browser_ctx, EMPTY_RESULTS)) is False


def test_a_blocked_render_is_not_retried(browser_ctx, monkeypatch):
    """The budget saving: three traversals become one."""
    import browser.playwright_scraper as ps

    attempts = {"n": 0}

    def fake_scrape_once(company, url, attempt):
        attempts["n"] += 1
        return ps.PlaywrightResult(jobs=[], blocked=True)

    monkeypatch.setattr(ps, "_scrape_once", fake_scrape_once)

    result = ps.scrape_with_playwright("Acme", "https://example.test/")

    assert result.blocked is True
    assert attempts["n"] == 1, "a blocked site was retried with a new fingerprint"


def test_a_clean_empty_render_is_still_retried(browser_ctx, monkeypatch):
    """The flaky-render retry must survive - it exists for a measured reason."""
    import browser.playwright_scraper as ps

    attempts = {"n": 0}

    def fake_scrape_once(company, url, attempt):
        attempts["n"] += 1
        return ps.PlaywrightResult(jobs=[])

    monkeypatch.setattr(ps, "_scrape_once", fake_scrape_once)
    monkeypatch.setattr(ps.time, "sleep", lambda *a: None)

    ps.scrape_with_playwright("Acme", "https://example.test/")

    assert attempts["n"] > 1
