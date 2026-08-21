"""Offline tests for the Jobvite collector.

No network: parsing runs against a trimmed fixture of the real
``jobs.jobvite.com/{tenant}/`` landing page, and ``collect`` is exercised by
monkeypatching the fetch.
"""

import pytest

from ats.base import CollectorUnavailable
from ats.jobvite import JOBVITE, JobviteCollector

# Two real FirstCash rows plus a duplicate of the first (same href) to prove
# dedupe, and a non-job nav anchor that must be ignored. Trimmed to the markup
# the parser reads: per-category <table class="jv-job-list"> of <tr> rows.
LANDING_HTML = """
<html><body>
<div class="jv-wrapper">
  <table class="jv-job-list">
    <tr>
      <td class="jv-job-list-name">
        <a href="/firstcash-holdings-inc/job/oLjEufwW">Retail Sales Associate - 3285</a>
      </td>
      <td class="jv-job-list-location">Center Point,
            Alabama</td>
    </tr>
    <tr>
      <td class="jv-job-list-name">
        <a href="/firstcash-holdings-inc/job/oZWhufwq">Store Manager</a>
      </td>
      <td class="jv-job-list-location">Birmingham,
            Alabama</td>
    </tr>
  </table>
  <table class="jv-job-list">
    <tr>
      <td class="jv-job-list-name">
        <a href="/firstcash-holdings-inc/job/oLjEufwW">Retail Sales Associate - 3285</a>
      </td>
      <td class="jv-job-list-location">Center Point, Alabama</td>
    </tr>
  </table>
  <div class="jv-footer">
    <a href="/firstcash-holdings-inc/">All Jobs</a>
  </div>
</div>
</body></html>
"""

EMPTY_HTML = """
<html><body>
<div class="jv-wrapper">
  <div class="jv-text-center">No open positions.</div>
</div>
</body></html>
"""


def _collector():
    detection = {
        "provider": JOBVITE,
        "url": "https://jobs.jobvite.com/firstcash-holdings-inc/",
        "host": "jobs.jobvite.com",
        "tenant": "firstcash-holdings-inc",
    }
    return JobviteCollector("FirstCash Holdings", detection)


def test_parse_rows_extracts_title_location_and_absolute_url():
    rows = _collector()._parse_rows(LANDING_HTML)
    manager = next(r for r in rows if r["title"] == "Store Manager")
    assert manager["location"] == "Birmingham, Alabama"   # whitespace collapsed
    assert manager["job_url"] == (
        "https://jobs.jobvite.com/firstcash-holdings-inc/job/oZWhufwq"
    )
    assert manager["ats_provider"] == JOBVITE
    assert manager["date_posted"] is None                 # list page carries no date


def test_parse_rows_ignores_non_job_anchors():
    rows = _collector()._parse_rows(LANDING_HTML)
    # The "All Jobs" footer link and category tables both live in the fixture;
    # only real /job/ rows survive.
    assert all("/job/" in r["job_url"] for r in rows)


def test_collect_dedupes_repeated_job_urls(monkeypatch):
    # The fixture repeats job oLjEufwW in a second category table.
    monkeypatch.setattr(JobviteCollector, "_fetch", lambda self, url: LANDING_HTML)
    jobs = _collector().collect()
    urls = [j["job_url"] for j in jobs]
    assert len(urls) == len(set(urls))
    assert len(jobs) == 2   # 3 rows, one is a duplicate


def test_collect_raises_when_page_has_no_jobs(monkeypatch):
    monkeypatch.setattr(JobviteCollector, "_fetch", lambda self, url: EMPTY_HTML)
    with pytest.raises(CollectorUnavailable):
        _collector().collect()


def test_collect_raises_when_fetch_fails(monkeypatch):
    def boom(self, url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(JobviteCollector, "_fetch", boom)
    with pytest.raises(CollectorUnavailable):
        _collector().collect()


def test_slug_falls_back_to_url_path():
    c = JobviteCollector("Tyler Technologies", {
        "provider": JOBVITE,
        "url": "https://jobs.jobvite.com/tyler-technologies/",
        "host": "jobs.jobvite.com",
    })
    assert c._slug() == "tyler-technologies"
