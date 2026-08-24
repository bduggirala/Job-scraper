"""Regression coverage for job links whose href carries no job keyword.

Real bug found live: Motion Recruitment's job list
(motionrecruitment.com/tech-jobs/data-engineering) links each posting as
/tech-jobs/{city}/{type}/{slug}/{id} - no "/job/", "jobId=", "/posting/", or
any other keyword the existing JOB_LINK_SELECTORS look for. The only thing
that identifies a listing as a job card is its container's CSS-module class
("JobItem_module_jobItem"). Confirmed live: 20 of 20 cards on the page
extracted as zero before adding a container-class selector.
"""

from browser.playwright_scraper import _extract_job_rows

HTML = """
<html><body>
  <div class="JobsList_module_list">
    <div class="JobItem_module_jobItem">
      <a href="/tech-jobs/dallas/contract/senior-data-engineer/883257">Senior Data Engineer</a>
    </div>
    <div class="JobItem_module_jobItem">
      <a href="/tech-jobs/irving/contract/data-architect/883258">Data Architect</a>
    </div>
  </div>
  <a href="/about">About Us</a>
</body></html>
"""


def test_extracts_job_cards_identified_only_by_container_class():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(HTML)
        rows = _extract_job_rows(page)
        browser.close()

    titles = {r["title"] for r in rows}
    assert "Senior Data Engineer" in titles
    assert "Data Architect" in titles
    assert "About Us" not in titles
