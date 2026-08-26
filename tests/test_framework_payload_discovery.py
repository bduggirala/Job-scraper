"""A house-built careers site names its payload whatever it likes.

The framework-data tier knew four names - ``__NUXT__``,
``__INITIAL_STATE__``, ``__APP_STATE__``, ``__PRELOADED_STATE__`` - plus the
Next.js script tag. That covers sites built on a framework convention and
nothing else, and "nothing else" is a large share of enterprise careers sites:
Paylocity ships an entire job board as ``window.pageData``, which this tier
walked straight past on its way to an expensive and ultimately empty browser
render.

So any ``window.<identifier> = {...}`` is now a candidate. The safety is not in
the name - it is in what happens next: the blob must parse as JSON, and the
walk only emits objects carrying both a title-ish and a URL-ish key. The tests
below are mostly about the things that must *not* become jobs.
"""

from __future__ import annotations

import json

import pytest

import ats.framework_data as fd
from ats.base import CollectorUnavailable
from ats.framework_data import FrameworkDataCollector

URL = "https://careers.example.com/jobs"


def _collect(monkeypatch, html):
    monkeypatch.setattr(fd.http_client, "get_text", lambda *a, **k: html)
    return FrameworkDataCollector("Acme", {"url": URL}).collect()


def _jobs(n, prefix="Data Engineer"):
    return [
        {"title": f"{prefix} {i}", "url": f"https://careers.example.com/job/{i}",
         "location": "Dallas, TX", "postedDate": "2026-08-25"}
        for i in range(n)
    ]


def _assign(name, obj):
    return f"<html><body><script>window.{name} = {json.dumps(obj)};</script></body></html>"


# --- the widening ----------------------------------------------------------

def test_an_arbitrarily_named_payload_is_read(monkeypatch):
    result = _collect(monkeypatch, _assign("pageData", {"Jobs": _jobs(12)}))
    assert len(result.jobs) == 12


@pytest.mark.parametrize("name", [
    "pageData", "jobBoardState", "APP_DATA", "_careersModel", "$store", "siteData",
])
def test_the_name_itself_does_not_matter(monkeypatch, name):
    result = _collect(monkeypatch, _assign(name, {"results": _jobs(11)}))
    assert len(result.jobs) == 11


def test_the_known_framework_names_still_work(monkeypatch):
    result = _collect(monkeypatch, _assign("__NUXT__", {"data": [{"jobs": _jobs(11)}]}))
    assert len(result.jobs) == 11


def test_a_named_payload_is_read_however_small_it_is(monkeypatch):
    """The speculative size floor must not apply to a real hydration payload."""
    result = _collect(monkeypatch, _assign("__NUXT__", {"j": _jobs(1)}))
    assert len(result.jobs) == 1


# --- what must not become a job -------------------------------------------

def test_an_analytics_payload_contributes_nothing(monkeypatch):
    """Present on nearly every careers page, and shaped just like a payload."""
    html = _assign("dataLayer", {
        "pageCategory": "careers", "event": "pageview",
        "products": [{"name": "Widget", "url": "/w", "price": 9} for _ in range(30)],
    })
    with pytest.raises(CollectorUnavailable):
        _collect(monkeypatch, html)


def test_articles_and_nav_entries_are_not_jobs(monkeypatch):
    """A title with no URL, or a URL with no title, is not a posting."""
    html = _assign("siteData", {
        "breadcrumbs": [{"url": f"/c/{i}"} for i in range(40)],
        "headings": [{"title": f"Section {i}"} for i in range(40)],
    })
    with pytest.raises(CollectorUnavailable):
        _collect(monkeypatch, html)


def test_a_tiny_config_stub_is_ignored(monkeypatch):
    """Below the floor: a feature flag, not a board."""
    html = _assign("cfg", {"a": 1})
    with pytest.raises(CollectorUnavailable):
        _collect(monkeypatch, html)


def test_unparseable_javascript_is_skipped_not_fatal(monkeypatch):
    html = ("<script>window.pageData = {this: is not json,};</script>"
            + _assign("realData", {"jobs": _jobs(11)}))
    assert len(_collect(monkeypatch, html).jobs) == 11


# --- bounds ----------------------------------------------------------------

def test_the_number_of_candidates_is_bounded(monkeypatch):
    """Balanced-brace scanning is linear per candidate; the total must not
    become quadratic just because a page carries a hundred assignments."""
    noise = "".join(
        f"<script>window.cfg{i} = {json.dumps({'k': 'v' * 300})};</script>"
        for i in range(200)
    )
    parsed: list[int] = []
    real = fd._parse_blob

    def counting(html_text, brace, **kw):
        parsed.append(brace)
        return real(html_text, brace, **kw)

    monkeypatch.setattr(fd, "_parse_blob", counting)
    with pytest.raises(CollectorUnavailable):
        _collect(monkeypatch, f"<html><body>{noise}</body></html>")

    assert len(parsed) <= fd._MAX_CANDIDATES


def test_a_description_containing_braces_does_not_truncate_the_payload(monkeypatch):
    jobs = _jobs(11)
    for job in jobs:
        job["description"] = "salary {min: 1, max: 2} and }{ stray braces"
    assert len(_collect(monkeypatch, _assign("pageData", {"Jobs": jobs})).jobs) == 11


def test_the_same_blob_is_not_parsed_twice(monkeypatch):
    """``window.a = window.b = {...}`` and duplicated tags both point at one brace."""
    blob = json.dumps({"jobs": _jobs(11)})
    html = (f"<script>window.__NUXT__ = {blob};</script>"
            f"<script>window.__NUXT__ = {blob};</script>")
    result = _collect(monkeypatch, html)
    assert len(result.jobs) == 11, "de-duplicated by URL, and parsed once per brace"
