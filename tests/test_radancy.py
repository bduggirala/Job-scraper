"""Offline tests for the Radancy TalentBrew collector and its detection.

No network: card parsing runs against a fixture fragment shaped like the real
``/search-jobs/results`` response, and detection runs against fixture HTML.
"""

import json

from ats.detector import RADANCY, detect_from_html
from ats.radancy import RadancyCollector

# One real 7-Eleven card plus a second, trimmed to the markup the parser reads.
RESULTS_FRAGMENT = """
<ul>
  <li>
    <a href="/job/mattydale/store-crew/45445/80924859120" data-job-id="80924859120">
      <div>
        <h2>Store Crew</h2>
        <span class="job-id job-info"><b>Address</b>
          <span>2723 Brewerton Road, Mattydale, New York, 13211, United States</span>
        </span>
        <span class="job-date job-location job-info"><b>Location</b> Mattydale, NY</span>
      </div>
    </a>
  </li>
  <li>
    <a href="/job/dallas/data-engineer/45445/99999999999" data-job-id="99999999999">
      <div>
        <h2>Senior Data Engineer</h2>
        <span class="job-date job-location job-info"><b>Location</b> Dallas, TX</span>
      </div>
    </a>
  </li>
</ul>
"""

DETECT_HTML = """
<html><head>
<link rel="preconnect" href="https://tbcdn.talentbrew.com"/>
</head><body>
<section data-search-filters-module-name="Search Filters"></section>
</body></html>
"""


def _collector():
    detection = {
        "provider": RADANCY,
        "url": "https://careers.7-eleven.com/search-jobs/",
        "host": "careers.7-eleven.com",
    }
    return RadancyCollector("7-Eleven", detection)


def test_detects_radancy_from_talentbrew_fingerprint():
    assert detect_from_html(DETECT_HTML, final_url="https://careers.7-eleven.com/") == RADANCY


def test_plain_marketing_page_is_not_radancy():
    html = "<html><body><h1>Life at Acme</h1><a href='/about'>About</a></body></html>"
    assert detect_from_html(html, final_url="https://www.acme.com/careers/") != RADANCY


def test_parse_cards_extracts_title_location_and_absolute_url():
    rows = _collector()._parse_cards(RESULTS_FRAGMENT, "https://careers.7-eleven.com/search-jobs/results")
    assert len(rows) == 2

    engineer = next(r for r in rows if r["title"] == "Senior Data Engineer")
    assert engineer["location"] == "Dallas, TX"          # "Location" label stripped
    assert engineer["job_url"] == (
        "https://careers.7-eleven.com/job/dallas/data-engineer/45445/99999999999"
    )
    assert engineer["ats_provider"] == RADANCY
    assert engineer["date_posted"] is None               # list page carries no date


def test_parse_cards_ignores_non_job_anchors():
    fragment = '<a data-job-id="1" href="/about-us">Not a job</a>'
    assert _collector()._parse_cards(fragment, "https://careers.7-eleven.com/") == []


def test_collect_paginates_and_stops_when_no_new_ids(monkeypatch):
    # Page 1 returns the fragment; every later page repeats it (no new ids),
    # so collect() must stop after de-duping rather than loop to MAX_PAGES.
    calls = {"n": 0}

    def fake_fetch(self, results_url, page):
        calls["n"] += 1
        return json.dumps({"results": RESULTS_FRAGMENT, "hasJobs": True})

    monkeypatch.setattr(RadancyCollector, "_fetch_page", fake_fetch)
    jobs = _collector().collect().jobs
    assert len(jobs) == 2
    assert calls["n"] == 2   # page 1 (2 new) + page 2 (0 new -> stop)


def test_collect_raises_when_endpoint_gives_nothing(monkeypatch):
    from ats.base import CollectorUnavailable
    import pytest

    monkeypatch.setattr(RadancyCollector, "_fetch_page", lambda self, u, p: "")
    with pytest.raises(CollectorUnavailable):
        _collector().collect()
