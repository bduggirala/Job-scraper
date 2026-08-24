"""Regression coverage for hash-routed SPA job links.

Real bug found live on two different frameworks. Kforce's job search
(kforce.com/find-work/search-jobs/) is a single-page app whose entire
routing is client-side hash fragments - every job card is
`<a class="linkForJob" href="#/detail/{id}/">`, with no server-side URL at
all. Mphasis's RippleHire-powered search (reached via a stable tokened URL,
mphasis.ripplehire.com/candidate/?token=...) uses the same pattern but
*without* a leading slash: `<a class="job-title" href="#detail/job/{id}">`.
`_is_job_row` rejected every href starting with "#" to filter out same-page
anchors ("#", "#top"), which also silently threw away every real job on
both sites. The general rule: a bare "#" or "#section-name" is chrome, but
any hash href containing a "/" has real path segments and is a route.
"""

from browser.playwright_scraper import _extract_job_rows, _is_job_row

HTML = """
<html><body>
  <h2><a class="linkForJob" href="#/detail/abc123/">Senior Data Engineer</a></h2>
  <h2><a class="linkForJob" href="#/detail/def456/">Data Analyst</a></h2>
  <li><a class="job-title" href="#detail/job/903342">Sr. Cloud Consultant</a></li>
  <a href="#">Skip to content</a>
  <a href="#top">Back to top</a>
</body></html>
"""


def test_is_job_row_accepts_hash_routed_spa_link_with_leading_slash():
    assert _is_job_row("Senior Data Engineer", "#/detail/abc123/") is True


def test_is_job_row_accepts_hash_routed_spa_link_without_leading_slash():
    assert _is_job_row("Sr. Cloud Consultant", "#detail/job/903342") is True


def test_is_job_row_rejects_bare_and_section_anchors():
    assert _is_job_row("Skip to content", "#") is False
    assert _is_job_row("Back to top", "#top") is False


def test_extracts_hash_routed_job_cards_from_a_real_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(HTML)
        rows = _extract_job_rows(page)
        browser.close()

    titles = {r["title"] for r in rows}
    assert "Senior Data Engineer" in titles
    assert "Data Analyst" in titles
    assert "Sr. Cloud Consultant" in titles
    assert "Skip to content" not in titles
    assert "Back to top" not in titles
