import ats.discovery as discovery
from ats.discovery import discover


def test_returns_not_found_when_there_is_no_seed():
    result = discover("Acme", None, use_browser=False)
    assert result.company == "Acme"
    assert result.ats_url is None
    assert result.jobs_found == 0
    assert result.method == "none"


def test_http_stage_finds_and_verifies_an_ats(monkeypatch):
    page = (
        '<link href="https://fa-x.fa.ocs.oraclecloud.com:443/hcmRestApi/'
        'CandidateExperience/siteFavicon/favicon-16x16.png?siteNumber=CX_1">'
    )
    monkeypatch.setattr(discovery, "_fetch", lambda url: page)
    monkeypatch.setattr(discovery, "verify_ats_url", lambda c, u: (575, "taleo API returned 575 jobs"))

    result = discover("Nokia", "https://jobs.nokia.com/en/sites/CX_1/jobs", use_browser=False)
    assert result.jobs_found == 575
    assert result.ats_url is not None
    assert "oraclecloud.com" in result.ats_url
    assert result.method == "http"


def test_unverifiable_candidate_is_not_written(monkeypatch):
    page = '<link href="https://fa-x.fa.ocs.oraclecloud.com:443/hcmRestApi/x?siteNumber=CX_1">'
    monkeypatch.setattr(discovery, "_fetch", lambda url: page)
    monkeypatch.setattr(discovery, "verify_ats_url", lambda c, u: (0, "taleo collector returned zero jobs"))

    result = discover("Nokia", "https://jobs.nokia.com/en/sites/CX_1/jobs", use_browser=False)
    assert result.ats_url is None
    assert result.jobs_found == 0
    assert result.method == "none"
    assert "zero jobs" in result.note


def test_fetch_failure_is_contained(monkeypatch):
    def boom(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(discovery, "_fetch", boom)
    result = discover("Acme", "https://acme.com/careers", use_browser=False)
    assert result.method == "none"
    assert result.jobs_found == 0


def test_candidate_extraction_failure_is_contained(monkeypatch):
    monkeypatch.setattr(discovery, "_fetch", lambda url: "<html></html>")

    def boom(html, base_url):
        raise ValueError("garbled markup")

    monkeypatch.setattr(discovery, "candidates_from_html", boom)
    result = discover("Acme", "https://acme.com/careers", use_browser=False)
    assert result.method == "none"
    assert result.jobs_found == 0


def test_verification_failure_is_contained(monkeypatch):
    monkeypatch.setattr(discovery, "_fetch", lambda url: "<html></html>")
    monkeypatch.setattr(discovery, "candidates_from_html", lambda html, base_url: ["https://ats.example.com/x"])

    def boom(company, url):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(discovery, "verify_ats_url", boom)
    result = discover("Acme", "https://acme.com/careers", use_browser=False)
    assert result.method == "none"
    assert result.jobs_found == 0


def test_link_extraction_failure_is_contained(monkeypatch):
    monkeypatch.setattr(discovery, "_fetch", lambda url: "<html></html>")
    monkeypatch.setattr(discovery, "candidates_from_html", lambda html, base_url: [])

    def boom(html, base_url):
        raise ValueError("garbled markup")

    monkeypatch.setattr(discovery, "careers_links", boom)
    result = discover("Acme", "https://acme.com/careers", use_browser=False)
    assert result.method == "none"
    assert result.jobs_found == 0
