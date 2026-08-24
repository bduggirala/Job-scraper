"""Regression coverage for job cards whose only link is a generic CTA.

Real bug found live: Pyramid Consulting's job board (jobs.sprockets.ai)
renders each card as a sibling <h3> title plus a separate <a><button>Apply
Now</button></a> - the link itself carries no title text. _extract_job_rows
always used the anchor's own innerText, so all 72 real postings on the page
extracted as the literal string "Apply Now" x72, which _is_job_row then
(correctly) discarded as nav chrome - losing every job on a card layout
that puts the title outside the link entirely.
"""

from browser.playwright_scraper import _extract_job_rows

HTML = """
<html><body>
  <li class="flex w-full flex-col gap-3 py-4">
    <div class="flex flex-wrap items-center gap-2">
      <h3 class="text-xl text-gray-900 font-semibold">Ab Initio Data Engineer</h3>
      <span>Full Time</span>
    </div>
    <div><span>Cupertino, CA</span></div>
    <a href="/en-US/pyramidinc/jobs/2b35ee04-a26e-4410-a148-1cecad448b3b">
      <button>Apply Now</button>
    </a>
  </li>
  <li class="flex w-full flex-col gap-3 py-4">
    <div class="flex flex-wrap items-center gap-2">
      <h3 class="text-xl text-gray-900 font-semibold">Senior Data Engineer</h3>
    </div>
    <a href="/en-US/pyramidinc/jobs/c6cf7ccd-e0eb-4744-a4eb-6e61a08b4318">
      <button>Apply Now</button>
    </a>
  </li>
</body></html>
"""


def test_falls_back_to_a_nearby_heading_when_link_text_is_a_generic_cta():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(HTML)
        rows = _extract_job_rows(page)
        browser.close()

    titles = {r["title"] for r in rows}
    assert "Ab Initio Data Engineer" in titles
    assert "Senior Data Engineer" in titles
    assert "Apply Now" not in titles
