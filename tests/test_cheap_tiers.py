"""The two cheap tiers that sat between JSON-LD and the browser, unbuilt.

The intended ladder is: ATS API -> embedded config -> static HTML -> JSON-LD ->
framework data -> browser. Two rungs were missing, so a server-rendered job
list on an unrecognised provider went straight to Playwright - the most
expensive path, capped at 3 workers, for a page a single GET could have read.

Both are provider-agnostic on purpose. Writing a per-provider collector for
every long-tail careers site does not scale; recognising the *shape* does.
"""

import json

import pytest

from ats.base import CollectorUnavailable
from ats.framework_data import FrameworkDataCollector
from ats.static_html import StaticHTMLCollector

LIST_HTML = """
<html><body>
  <ul class="jobs">
    <li><a href="/careers/job/1001">Senior Data Engineer</a>
        <span class="location">Plano, TX</span></li>
    <li><a href="/careers/job/1002">Data Platform Engineer</a>
        <span class="location">Dallas, TX</span></li>
    <li><a href="/careers/job/1003">Analytics Engineer</a>
        <span class="location">Irving, TX</span></li>
  </ul>
</body></html>
"""

NAV_ONLY_HTML = """
<html><body>
  <a href="/about">About us</a>
  <a href="/careers">Careers</a>
  <a href="/contact">Contact</a>
</body></html>
"""

NEXT_DATA = {
    "props": {"pageProps": {"jobs": [
        {"title": "Senior Data Engineer", "location": "Plano, TX",
         "url": "https://acme.test/jobs/1", "datePosted": "2026-08-20"},
        {"title": "Data Platform Engineer", "location": "Dallas, TX",
         "url": "https://acme.test/jobs/2"},
    ]}}
}

NEXT_HTML = (
    '<html><body><div id="__next"></div>'
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(NEXT_DATA)
    + "</script></body></html>"
)

NUXT_HTML = (
    "<html><body><script>window.__NUXT__ = "
    + json.dumps({"data": [{"openings": [
        {"title": "ETL Developer", "city": "Richardson, TX",
         "applyUrl": "https://acme.test/jobs/9"}
    ]}]})
    + ";</script></body></html>"
)


def _collector(cls, html, monkeypatch, url="https://acme.test/careers"):
    import http_client
    monkeypatch.setattr(http_client, "get_text", lambda u, **kw: html)
    return cls("Acme", {"provider": "unknown", "url": url})


# --- static HTML -----------------------------------------------------------

def test_a_server_rendered_list_is_harvested_over_one_get(monkeypatch):
    result = _collector(StaticHTMLCollector, LIST_HTML, monkeypatch).collect()

    titles = {j["title"] for j in result.jobs}
    assert "Senior Data Engineer" in titles
    assert len(result.jobs) == 3


def test_relative_links_become_absolute(monkeypatch):
    result = _collector(StaticHTMLCollector, LIST_HTML, monkeypatch).collect()
    assert all(j["job_url"].startswith("https://acme.test/") for j in result.jobs)


def test_a_page_of_navigation_is_not_mistaken_for_a_job_list(monkeypatch):
    """Escalating to the browser is right here; inventing three jobs is not."""
    with pytest.raises(CollectorUnavailable):
        _collector(StaticHTMLCollector, NAV_ONLY_HTML, monkeypatch).collect()


def test_a_fetch_failure_escalates_rather_than_raising(monkeypatch):
    import http_client

    def boom(url, **kw):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(http_client, "get_text", boom)
    collector = StaticHTMLCollector("Acme", {"provider": "unknown",
                                             "url": "https://acme.test/careers"})
    with pytest.raises(CollectorUnavailable):
        collector.collect()


# --- framework data --------------------------------------------------------

def test_next_data_jobs_are_extracted(monkeypatch):
    result = _collector(FrameworkDataCollector, NEXT_HTML, monkeypatch).collect()

    titles = {j["title"] for j in result.jobs}
    assert titles == {"Senior Data Engineer", "Data Platform Engineer"}


def test_next_data_carries_the_posting_date(monkeypatch):
    result = _collector(FrameworkDataCollector, NEXT_HTML, monkeypatch).collect()
    dated = [j for j in result.jobs if j["date_posted"]]
    assert dated, "a real datePosted in the payload was dropped"


def test_nuxt_state_is_also_read(monkeypatch):
    result = _collector(FrameworkDataCollector, NUXT_HTML, monkeypatch).collect()
    assert {j["title"] for j in result.jobs} == {"ETL Developer"}


def test_a_page_with_no_framework_payload_escalates(monkeypatch):
    with pytest.raises(CollectorUnavailable):
        _collector(FrameworkDataCollector, LIST_HTML, monkeypatch).collect()


def test_a_framework_payload_with_no_job_shaped_objects_escalates(monkeypatch):
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": {"articles": [{"headline": "x"}]}}})
            + "</script>")
    with pytest.raises(CollectorUnavailable):
        _collector(FrameworkDataCollector, html, monkeypatch).collect()
