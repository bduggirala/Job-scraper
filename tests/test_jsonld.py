"""Offline tests for the generic schema.org JobPosting JSON-LD collector.

No network: ``http_client.get_text`` is monkeypatched to return fixture HTML
shaped like the ``application/ld+json`` blocks real career pages emit. The
fixtures exercise every shape the collector must tolerate: a single object, a
list, an ``@graph`` wrapper, a page with no JSON-LD, and a malformed block
coexisting with a valid one.
"""

import pytest

import http_client
from ats.base import CollectorUnavailable
from ats.jsonld import JSONLD, JSONLDCollector, page_has_jobposting_jsonld

# (a) One JobPosting as a single top-level object.
SINGLE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "Staff Software Engineer",
  "datePosted": "2026-08-01",
  "employmentType": "FULL_TIME",
  "url": "https://jobs.acme.com/postings/staff-swe-123",
  "description": "<p>Build <b>great</b> things.</p>",
  "jobLocation": {
    "@type": "Place",
    "address": {
      "@type": "PostalAddress",
      "addressLocality": "Austin",
      "addressRegion": "TX",
      "addressCountry": "US"
    }
  }
}
</script>
</head><body>...</body></html>
"""

# (b) A list of JobPostings.
LIST = """
<html><body>
<script type="application/ld+json">
[
  {
    "@type": "JobPosting",
    "title": "Backend Engineer",
    "datePosted": "2026-07-15",
    "url": "https://jobs.acme.com/postings/backend-1",
    "jobLocation": {"address": {"addressLocality": "Remote", "addressCountry": "US"}}
  },
  {
    "@type": "JobPosting",
    "title": "Frontend Engineer",
    "datePosted": "2026-07-20",
    "@id": "https://jobs.acme.com/postings/frontend-2",
    "jobLocation": {"address": {"addressLocality": "Denver", "addressRegion": "CO"}}
  }
]
</script>
</body></html>
"""

# (c) An @graph wrapper with a JobPosting among unrelated entity types.
GRAPH = """
<html><body>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {"@type": "Organization", "name": "Acme Corp"},
    {"@type": "BreadcrumbList", "itemListElement": []},
    {
      "@type": "JobPosting",
      "title": "Data Scientist",
      "datePosted": "2026-08-10",
      "url": "https://jobs.acme.com/postings/ds-77",
      "jobLocation": [
        {"address": {"addressLocality": "New York", "addressRegion": "NY", "addressCountry": "US"}}
      ]
    }
  ]
}
</script>
</body></html>
"""

# (d) No JSON-LD at all.
NONE = """
<html><body><h1>Careers at Acme</h1><a href="/jobs">See openings</a></body></html>
"""

# (f) A JobPosting that carries its title under ``name`` rather than ``title``.
NAME_ONLY = """
<html><body>
<script type="application/ld+json">
{
  "@type": "JobPosting",
  "name": "Lead Data Engineer",
  "datePosted": "2026-08-12",
  "url": "https://jobs.acme.com/postings/lde-42",
  "jobLocation": {"address": {"addressLocality": "Dallas", "addressRegion": "TX"}}
}
</script>
</body></html>
"""

# (e) A malformed JSON block alongside a valid JobPosting.
MALFORMED_PLUS_VALID = """
<html><body>
<script type="application/ld+json">
{ this is not valid json, "title": }
</script>
<script type="application/ld+json">
{
  "@type": "JobPosting",
  "title": "Product Manager",
  "datePosted": "2026-08-05",
  "url": "https://jobs.acme.com/postings/pm-9",
  "jobLocation": {"address": {"addressLocality": "Seattle", "addressRegion": "WA"}}
}
</script>
</body></html>
"""


def _collector():
    detection = {
        "provider": JSONLD,
        "url": "https://jobs.acme.com/postings/some-page",
    }
    return JSONLDCollector("Acme", detection)


def _patch_get_text(monkeypatch, html):
    monkeypatch.setattr(http_client, "get_text", lambda url, **kw: html)


def test_single_jobposting_object(monkeypatch):
    _patch_get_text(monkeypatch, SINGLE)
    rows = _collector().collect()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Staff Software Engineer"
    assert row["location"] == "Austin, TX, US"
    assert row["job_url"] == "https://jobs.acme.com/postings/staff-swe-123"
    assert row["date_posted"].startswith("2026-08-01")
    assert row["employment_type"] == "FULL_TIME"
    assert row["ats_provider"] == JSONLD
    # description HTML is stripped to plain text.
    assert row["description"] == "Build great things."


def test_list_of_jobpostings(monkeypatch):
    _patch_get_text(monkeypatch, LIST)
    rows = _collector().collect()
    assert len(rows) == 2
    titles = {r["title"] for r in rows}
    assert titles == {"Backend Engineer", "Frontend Engineer"}

    frontend = next(r for r in rows if r["title"] == "Frontend Engineer")
    # job_url falls back to @id when "url" is absent.
    assert frontend["job_url"] == "https://jobs.acme.com/postings/frontend-2"
    assert frontend["location"] == "Denver, CO"

    backend = next(r for r in rows if r["title"] == "Backend Engineer")
    assert backend["location"] == "Remote, US"
    assert backend["date_posted"].startswith("2026-07-15")


def test_graph_wrapper_extracts_only_jobposting(monkeypatch):
    _patch_get_text(monkeypatch, GRAPH)
    rows = _collector().collect()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Data Scientist"
    # jobLocation given as a list is handled.
    assert row["location"] == "New York, NY, US"
    assert row["job_url"] == "https://jobs.acme.com/postings/ds-77"
    assert row["date_posted"].startswith("2026-08-10")


def test_no_jsonld_raises_unavailable(monkeypatch):
    _patch_get_text(monkeypatch, NONE)
    with pytest.raises(CollectorUnavailable):
        _collector().collect()


def test_title_falls_back_to_name(monkeypatch):
    # A JobPosting whose title lives under ``name`` must still be collected,
    # not silently dropped for lacking a ``title`` key.
    _patch_get_text(monkeypatch, NAME_ONLY)
    rows = _collector().collect()
    assert len(rows) == 1
    assert rows[0]["title"] == "Lead Data Engineer"
    assert rows[0]["location"] == "Dallas, TX"


def test_malformed_block_skipped_valid_extracted(monkeypatch):
    _patch_get_text(monkeypatch, MALFORMED_PLUS_VALID)
    rows = _collector().collect()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Product Manager"
    assert row["location"] == "Seattle, WA"
    assert row["job_url"] == "https://jobs.acme.com/postings/pm-9"


def test_page_has_jobposting_jsonld_predicate():
    assert page_has_jobposting_jsonld(SINGLE) is True
    assert page_has_jobposting_jsonld(LIST) is True
    assert page_has_jobposting_jsonld(GRAPH) is True
    assert page_has_jobposting_jsonld(NONE) is False
    assert page_has_jobposting_jsonld(MALFORMED_PLUS_VALID) is True
    assert page_has_jobposting_jsonld("") is False


def test_fetch_failure_raises_unavailable(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(http_client, "get_text", boom)
    with pytest.raises(CollectorUnavailable):
        _collector().collect()
