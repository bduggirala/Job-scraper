import pytest

from ats.base import CollectorUnavailable
from ats.taleo import TaleoCollector


def test_both_failures_are_reported(monkeypatch):
    collector = TaleoCollector(
        "Example", {"url": "https://ex.taleo.net/careersection/2/jobsearch.ftl"}
    )

    def fail_legacy():
        raise CollectorUnavailable("Taleo searchjobs returned zero requisitions")

    def fail_orc():
        raise CollectorUnavailable("Oracle Cloud API unavailable: HTTP 404")

    monkeypatch.setattr(collector, "_collect_legacy_taleo", fail_legacy)
    monkeypatch.setattr(collector, "_collect_oracle_cloud", fail_orc)

    with pytest.raises(CollectorUnavailable) as excinfo:
        collector.collect()

    message = str(excinfo.value)
    assert "zero requisitions" in message
    assert "HTTP 404" in message
