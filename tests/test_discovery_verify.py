import pytest

from ats.base import CollectorUnavailable
from ats.discovery import NOT_FOUND, Discovery, verify_ats_url


def test_not_found_constant():
    assert NOT_FOUND == "NOT FOUND"


def test_discovery_defaults_to_nothing_found():
    d = Discovery(company="Acme")
    assert d.ats_url is None
    assert d.jobs_page is None
    assert d.jobs_found == 0
    assert d.method == "none"


def test_verify_accepts_a_url_that_returns_jobs(monkeypatch):
    import ats.discovery as discovery

    class FakeCollector:
        provider = "greenhouse"

        def __init__(self, company, detection):
            pass

        def collect(self):
            return [{"title": "Data Engineer"}, {"title": "Analytics Engineer"}]

    monkeypatch.setitem(discovery.COLLECTORS, "greenhouse", FakeCollector)
    found, note = verify_ats_url("Acme", "https://boards.greenhouse.io/acme")
    assert found == 2
    assert "2 jobs" in note


def test_verify_rejects_a_url_that_returns_no_jobs(monkeypatch):
    import ats.discovery as discovery

    class EmptyCollector:
        provider = "greenhouse"

        def __init__(self, company, detection):
            pass

        def collect(self):
            return []

    monkeypatch.setitem(discovery.COLLECTORS, "greenhouse", EmptyCollector)
    found, note = verify_ats_url("Acme", "https://boards.greenhouse.io/acme")
    assert found == 0
    assert "zero jobs" in note


def test_verify_rejects_when_the_collector_raises(monkeypatch):
    import ats.discovery as discovery

    class BrokenCollector:
        provider = "greenhouse"

        def __init__(self, company, detection):
            pass

        def collect(self):
            raise CollectorUnavailable("board not found")

    monkeypatch.setitem(discovery.COLLECTORS, "greenhouse", BrokenCollector)
    found, note = verify_ats_url("Acme", "https://boards.greenhouse.io/acme")
    assert found == 0
    assert "board not found" in note


def test_verify_rejects_an_unrecognised_url():
    found, note = verify_ats_url("Acme", "https://www.acme.com/careers/")
    assert found == 0
    assert "no collector" in note.lower()
