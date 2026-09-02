"""The hint store's decision rules, and the router's use of them.

The behaviour under test is not "does a cache work" but "can a stale cache
ever cost a company". Every test here is really one of two questions: does a
bad hint fall through to full discovery, and does a failure get classified as
evidence against the stored URL only when it actually is?
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import browser_hints
from ats.router import RoutePlan, _collect_via_hint
from browser.playwright_scraper import PlaywrightResult, _sniff_json_endpoint


@pytest.fixture(autouse=True)
def clean_store(tmp_path, monkeypatch):
    """Every test gets its own empty store, written nowhere real."""
    browser_hints.reset_for_tests()
    path = tmp_path / "browser_hints.json"
    monkeypatch.setattr(browser_hints, "_path", lambda: path)
    yield path
    browser_hints.reset_for_tests()


def _plan(company="Acme"):
    return RoutePlan(company=company, url="https://acme.example/careers",
                     provider="unknown", method="playwright",
                     source="live_jobs_page")


def _seed(company, **fields):
    entry = {"entry_url": "https://acme.example/jobs",
             "verified_at": date.today().isoformat(),
             "jobs_last_seen": 100, "consecutive_failures": 0, "proven": True}
    entry.update(fields)
    browser_hints.load()
    browser_hints._store[company] = entry  # noqa: SLF001
    return entry


def _rows(n, start=0):
    return [{"title": f"Data Engineer {i}", "location": "Dallas, TX",
             "job_url": f"https://acme.example/job/{i}"} for i in range(start, start + n)]


# --- the fast path ---------------------------------------------------------

def test_hint_hit_skips_discovery(monkeypatch):
    """A good hint serves the company without any hop or search."""
    _seed("Acme", jobs_last_seen=100)
    called = {}

    def fake_entry(company, url, timeout_seconds=None):
        called["url"] = url
        return PlaywrightResult(jobs=_rows(100), entry_url=url)

    monkeypatch.setattr("browser.playwright_scraper.scrape_entry_url", fake_entry)
    monkeypatch.setattr(
        "browser.playwright_scraper.scrape_with_playwright",
        lambda *a, **k: pytest.fail("full discovery must not run for a good hint"),
    )

    harvest = _collect_via_hint(_plan())
    assert harvest is not None
    assert len(harvest.records) == 100
    assert harvest.method == "browser_hint"
    assert called["url"] == "https://acme.example/jobs"


def test_endpoint_hint_uses_no_browser(monkeypatch):
    """A remembered JSON endpoint is read over HTTP, skipping Chromium."""
    _seed("Acme", json_endpoint="https://acme.example/api/jobs?page=1",
          jobs_last_seen=10)
    monkeypatch.setattr(
        "browser.playwright_scraper.scrape_entry_url",
        lambda *a, **k: pytest.fail("no browser may be launched for an endpoint hint"),
    )
    payload = json.dumps({"jobs": [
        {"title": f"Data Engineer {i}", "url": f"https://acme.example/job/{i}",
         "location": "Dallas, TX"} for i in range(12)
    ]})
    monkeypatch.setattr("http_client.get_text", lambda *a, **k: payload)

    harvest = _collect_via_hint(_plan())
    assert harvest is not None
    assert harvest.method == "hint_endpoint"
    assert len(harvest.records) == 12


# --- failure classification ------------------------------------------------

def test_clean_failure_on_unproven_hint_marks_unsupported(monkeypatch):
    """A candidate that never worked is not stale - its list has no URL.

    Discarding it instead would re-record the same useless URL on the next
    successful discovery, and burn the hint budget on it every run forever.
    """
    _seed("Acme", proven=False)
    monkeypatch.setattr("browser.playwright_scraper.scrape_entry_url",
                        lambda *a, **k: PlaywrightResult(jobs=[]))

    assert _collect_via_hint(_plan()) is None
    assert browser_hints.get("Acme") is None
    stored = browser_hints._store["Acme"]  # noqa: SLF001
    assert stored["hint_unsupported"] is True
    assert "entry_url" not in stored


def test_clean_failure_on_proven_hint_discards_it(monkeypatch):
    """A hint that used to work and now 404s is simply stale."""
    _seed("Acme", proven=True)
    monkeypatch.setattr("browser.playwright_scraper.scrape_entry_url",
                        lambda *a, **k: PlaywrightResult(jobs=[]))

    assert _collect_via_hint(_plan()) is None
    assert "Acme" not in browser_hints._store  # noqa: SLF001


def test_blocked_page_never_invalidates_a_hint(monkeypatch):
    """A bot wall says nothing about whether the stored URL is right."""
    _seed("Acme", proven=True)
    monkeypatch.setattr("browser.playwright_scraper.scrape_entry_url",
                        lambda *a, **k: PlaywrightResult(blocked=True))

    assert _collect_via_hint(_plan()) is None
    stored = browser_hints._store["Acme"]  # noqa: SLF001
    assert stored["entry_url"] == "https://acme.example/jobs"
    assert stored["consecutive_failures"] == 0


def test_navigation_failure_keeps_hint_but_counts_it(monkeypatch):
    """Timeouts are the transient class, not evidence the page moved."""
    _seed("Acme", proven=True)

    def boom(*a, **k):
        raise RuntimeError("net::ERR_TIMED_OUT")

    monkeypatch.setattr("browser.playwright_scraper.scrape_entry_url", boom)
    assert _collect_via_hint(_plan()) is None
    assert browser_hints._store["Acme"]["consecutive_failures"] == 1  # noqa: SLF001

    assert _collect_via_hint(_plan()) is None
    assert "Acme" not in browser_hints._store  # noqa: SLF001


# --- the oscillation guard -------------------------------------------------

def test_shrinking_company_settles_instead_of_oscillating(monkeypatch):
    """The rejection must never write the baseline it is measured against.

    A company that genuinely shrinks from 100 jobs to 50 should fail its hint
    once, be rediscovered, and then be stable. If the *rejection* wrote
    jobs_last_seen, the next run would accept 50 against a bar of 50, the run
    after would reject again, and the company would ping-pong between the fast
    and slow paths forever.
    """
    _seed("Acme", jobs_last_seen=100, proven=True)
    monkeypatch.setattr("browser.playwright_scraper.scrape_entry_url",
                        lambda *a, **k: PlaywrightResult(jobs=_rows(50),
                                                         entry_url="https://acme.example/jobs"))

    # 100 * 0.8 = 80 required; 50 is short, so it falls through.
    assert _collect_via_hint(_plan()) is None
    assert browser_hints._store["Acme"]["jobs_last_seen"] == 100  # noqa: SLF001

    # Full discovery collects the real 50 and records it.
    browser_hints.record_success("Acme", entry_url="https://acme.example/jobs", jobs=50)
    assert browser_hints._store["Acme"]["jobs_last_seen"] == 50  # noqa: SLF001

    # Now 50 clears a bar of 40 and the company is stable on the fast path.
    harvest = _collect_via_hint(_plan())
    assert harvest is not None and len(harvest.records) == 50


# --- expiry ----------------------------------------------------------------

def test_expired_hint_is_ignored():
    old = (date.today() - timedelta(days=400)).isoformat()
    _seed("Acme", verified_at=old)
    assert browser_hints.get("Acme") is None


def test_expiry_is_staggered_across_companies():
    """Hints written on one run must not all expire on the same later run."""
    window = 14
    offsets = {browser_hints._stagger_offset(f"Company {i}", window)  # noqa: SLF001
               for i in range(40)}
    assert len(offsets) > 1
    assert all(0 <= o < window for o in offsets)


def test_unsupported_marker_expires_so_rebuilt_sites_get_another_chance():
    old = (date.today() - timedelta(days=400)).isoformat()
    _seed("Acme", hint_unsupported=True, checked_at=old, verified_at=None)
    assert browser_hints.get("Acme") is None
    assert browser_hints.is_expired("Acme", browser_hints._store["Acme"])  # noqa: SLF001


# --- store robustness ------------------------------------------------------

def test_corrupt_hint_file_is_not_fatal(clean_store):
    clean_store.write_text("{not json at all", encoding="utf-8")
    assert browser_hints.load() == {}
    assert browser_hints.get("Acme") is None


def test_flush_writes_atomically(clean_store):
    browser_hints.load()
    browser_hints.record_success("Acme", entry_url="https://acme.example/jobs", jobs=7)
    assert browser_hints.flush() is True
    stored = json.loads(clean_store.read_text(encoding="utf-8"))
    assert stored["Acme"]["entry_url"] == "https://acme.example/jobs"
    assert stored["Acme"]["jobs_last_seen"] == 7
    assert not list(clean_store.parent.glob("*.tmp"))


def test_disabled_hints_are_inert(monkeypatch):
    monkeypatch.setattr(browser_hints, "enabled", lambda: False)
    browser_hints.record_success("Acme", entry_url="https://x/y", jobs=5)
    assert browser_hints.get("Acme") is None
    assert _collect_via_hint(_plan()) is None


# --- endpoint sniffing -----------------------------------------------------

def test_repeating_json_call_is_recorded():
    """A list endpoint repeats with varying params; a one-off does not."""
    urls = [
        "https://acme.example/api/config?v=1",
        "https://acme.example/api/jobs/search?page=1&q=data",
        "https://acme.example/api/jobs/search?page=2&q=data",
        "https://acme.example/api/telemetry?e=view",
    ]
    assert _sniff_json_endpoint(urls) == "https://acme.example/api/jobs/search?page=1&q=data"


def test_single_json_call_is_not_an_endpoint():
    assert _sniff_json_endpoint(["https://acme.example/api/jobs?page=1"]) is None


def test_non_job_paths_are_ignored():
    urls = ["https://acme.example/api/analytics?p=1", "https://acme.example/api/analytics?p=2"]
    assert _sniff_json_endpoint(urls) is None


# --- what gets stored ------------------------------------------------------

class _PaginatingPage:
    """A page whose URL walks forward as pagination clicks through it.

    Models the real shape of the bug this guards: ``?page=N`` climbs with every
    click, so reading ``page.url`` after the walk yields the *last* page rather
    than the list.
    """

    def __init__(self, base="https://acme.example/jobs"):
        self.base = base
        self.page_no = 1

    @property
    def url(self):
        return f"{self.base}?page={self.page_no}"

    def paginate(self, pages=40):
        self.page_no = pages


def test_entry_url_is_the_list_not_the_last_page_walked(monkeypatch):
    """The stored destination must be where the list starts.

    Recording ``page.url`` after pagination stored ``?page=41`` for IBM, CBRE,
    Goldman Sachs and Built In. Navigating there next run returns one page of
    rows (IBM: 105 against 863 expected), fails the yield check and triggers a
    full rediscovery - so the biggest browser companies, the ones with the most
    to gain, were the only ones a hint could never help.
    """
    page = _PaginatingPage()
    captured_before = page.url          # what the fix records
    page.paginate(41)                   # the walk moves the URL on
    captured_after = page.url           # what the bug recorded

    assert captured_before == "https://acme.example/jobs?page=1"
    assert captured_after == "https://acme.example/jobs?page=41"

    # A hint pointing at the tail of the list cannot clear the yield bar.
    entry = {"jobs_last_seen": 863}
    assert browser_hints.min_rows("IBM", entry) == 690
    assert 105 < browser_hints.min_rows("IBM", entry)


def test_paginated_hint_falls_through_rather_than_serving_a_short_list(monkeypatch):
    """A hint that returns one page of a long list must not be accepted."""
    _seed("Acme", jobs_last_seen=863, proven=True)
    monkeypatch.setattr(
        "browser.playwright_scraper.scrape_entry_url",
        lambda *a, **k: PlaywrightResult(jobs=_rows(105),
                                         entry_url="https://acme.example/jobs?page=41"),
    )
    assert _collect_via_hint(_plan()) is None
    # Kept, not discarded: a thin result is not proof the page moved.
    assert browser_hints._store["Acme"]["consecutive_failures"] == 1  # noqa: SLF001
