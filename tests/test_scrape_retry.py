"""Regression test: scrape_with_playwright must retry a clean-but-empty render.

Same-day evidence from two full runs: Nokia, Ericsson and CBRE each rendered
without error and found real jobs in one run, then came back with zero jobs
in the other under the same 3-worker concurrency - re-verified individually
afterward, all three worked every time in isolation. Before this fix, a
single empty result (no exception raised) was accepted as final; only
navigation *errors* were retried.
"""

import browser.playwright_scraper as ps
from browser.playwright_scraper import PlaywrightResult


def test_retries_after_clean_empty_result(monkeypatch):
    calls = {"n": 0}

    def fake_scrape_once(company, url, attempt):
        calls["n"] += 1
        if calls["n"] == 1:
            return PlaywrightResult(jobs=[])  # clean render, nothing found
        return PlaywrightResult(jobs=[{"title": "Data Engineer"}])

    monkeypatch.setattr(ps, "_scrape_once", fake_scrape_once)
    monkeypatch.setattr(ps.time, "sleep", lambda *_: None)  # skip real backoff

    result = ps.scrape_with_playwright("Acme", "https://acme.example/careers")
    assert calls["n"] == 2
    assert len(result.jobs) == 1


def test_gives_up_after_all_attempts_still_empty(monkeypatch):
    calls = {"n": 0}

    def fake_scrape_once(company, url, attempt):
        calls["n"] += 1
        return PlaywrightResult(jobs=[])

    monkeypatch.setattr(ps, "_scrape_once", fake_scrape_once)
    monkeypatch.setattr(ps.time, "sleep", lambda *_: None)

    result = ps.scrape_with_playwright("Acme", "https://acme.example/careers")
    assert calls["n"] == 3  # default playwright.nav_retries
    assert result.jobs == []


def test_does_not_retry_when_jobs_found_on_first_attempt(monkeypatch):
    calls = {"n": 0}

    def fake_scrape_once(company, url, attempt):
        calls["n"] += 1
        return PlaywrightResult(jobs=[{"title": "Data Engineer"}])

    monkeypatch.setattr(ps, "_scrape_once", fake_scrape_once)

    result = ps.scrape_with_playwright("Acme", "https://acme.example/careers")
    assert calls["n"] == 1
    assert len(result.jobs) == 1


def test_does_not_retry_when_discovery_found_with_no_jobs(monkeypatch):
    """A discovered ATS link with zero scraped jobs is still a useful result."""
    calls = {"n": 0}

    def fake_scrape_once(company, url, attempt):
        calls["n"] += 1
        return PlaywrightResult(jobs=[], discovered_ats_url="https://x.wd5.myworkdayjobs.com/External",
                                 discovered_provider="workday")

    monkeypatch.setattr(ps, "_scrape_once", fake_scrape_once)

    result = ps.scrape_with_playwright("Acme", "https://acme.example/careers")
    assert calls["n"] == 1
    assert result.discovered_provider == "workday"
