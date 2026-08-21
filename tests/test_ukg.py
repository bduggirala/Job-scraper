"""Offline tests for the UKG Pro / UltiPro collector.

No network: ``http_client.post_json`` is monkeypatched. Covers the per-tenant
host bug where ``_base()`` ignored any detected host other than ``ukg.net``
and always POSTed to the shared ``recruiting.ultipro.com`` origin, 404ing on
tenants (like CorVel's ``recruiting2.ultipro.com``) served from a different
per-tenant UltiPro host.
"""

import ats.ukg as ukg_module
from ats.ukg import UKGCollector

PAGE_1 = {
    "totalCount": 1,
    "opportunities": [
        {
            "Id": "abc-123",
            "Title": "Payment Integrity Analyst II",
            "PostedDate": "2026-08-21T20:13:37.377000+00:00",
            "Locations": [{"LocalizedDescription": "TX - Fort Worth"}],
        }
    ],
}


def _collector(host: str):
    detection = {
        "provider": "ukg",
        "url": f"https://{host}/COR1025CVEL/JobBoard/661856a2-40b3-49f9-ab1e-9845cfac508d",
        "host": host,
        "tenant": "COR1025CVEL",
        "site": "661856a2-40b3-49f9-ab1e-9845cfac508d",
    }
    return UKGCollector("CorVel", detection)


def test_uses_detected_ultipro_host_not_shared_default(monkeypatch):
    """A tenant on recruiting2.ultipro.com must not be flattened to recruiting.ultipro.com."""
    seen_urls = []

    def fake_post_json(url, payload, **kwargs):
        seen_urls.append(url)
        return {"opportunities": [], "totalCount": 0}

    monkeypatch.setattr(ukg_module.http_client, "post_json", fake_post_json)
    collector = _collector("recruiting2.ultipro.com")
    try:
        collector.collect()
    except Exception:
        pass  # empty result raises CollectorUnavailable; only the endpoint matters here

    assert seen_urls, "collector never called post_json"
    assert seen_urls[0].startswith("https://recruiting2.ultipro.com/")


def test_still_uses_shared_host_when_none_detected(monkeypatch):
    seen_urls = []

    def fake_post_json(url, payload, **kwargs):
        seen_urls.append(url)
        return {"opportunities": [], "totalCount": 0}

    monkeypatch.setattr(ukg_module.http_client, "post_json", fake_post_json)
    detection = {
        "provider": "ukg", "url": None, "host": None,
        "tenant": "COR1025CVEL", "site": "661856a2-40b3-49f9-ab1e-9845cfac508d",
    }
    collector = UKGCollector("CorVel", detection)
    try:
        collector.collect()
    except Exception:
        pass

    assert seen_urls[0].startswith("https://recruiting.ultipro.com/")


def test_parses_real_shaped_response(monkeypatch):
    monkeypatch.setattr(ukg_module.http_client, "post_json", lambda url, payload, **kw: PAGE_1)
    rows = _collector("recruiting2.ultipro.com").collect()
    assert len(rows) == 1
    assert rows[0]["title"] == "Payment Integrity Analyst II"
    assert rows[0]["location"] == "TX - Fort Worth"
