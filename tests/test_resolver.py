"""Offline tests for ats.resolver.resolve_from_page.

Focus: the per-request 403 -> browser-UA escalation. The default session UA
stays bare (iCIMS depends on it), so the escalation lives only in the resolver
and must be scoped to the single retried GET.
"""

from __future__ import annotations

import http_client
from ats import resolver
from ats.detector import GREENHOUSE, UNKNOWN

# A branded page whose HTML embeds a concrete Greenhouse board. Fingerprinting
# this yields a real greenhouse detection with tenant coordinates.
GREENHOUSE_PAGE = (
    '<html><head><script '
    'src="https://boards.greenhouse.io/embed/job_board/js?for=acme">'
    '</script></head><body>'
    '<a href="https://boards.greenhouse.io/acme">Openings</a>'
    '</body></html>'
)


class _FakeResponse:
    """Minimal stand-in for the streamed requests.Response resolver reads."""

    def __init__(self, url: str, body: bytes):
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.raw = _FakeRaw(body)

    def close(self):
        pass


class _FakeRaw:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, amt, decode_content=True):
        return self._body


def test_403_then_browser_ua_retry_resolves(monkeypatch):
    calls = []

    def fake_request(url, method="GET", **kwargs):
        calls.append(kwargs.get("headers"))
        if len(calls) == 1:
            # First GET uses the default session UA (no per-request header).
            raise http_client.HTTPError("HTTP 403", status_code=403)
        return _FakeResponse(url, GREENHOUSE_PAGE.encode("utf-8"))

    monkeypatch.setattr(http_client, "request", fake_request)

    result = resolver.resolve_from_page("Acme", "https://careers.acme.com")

    assert result["provider"] == GREENHOUSE
    assert len(calls) == 2

    # First call did NOT carry a per-request User-Agent (used the bare default
    # session UA that iCIMS relies on).
    first_headers = calls[0] or {}
    assert "User-Agent" not in first_headers

    # Retry escalated to a full browser UA (the playwright UA from settings).
    retry_headers = calls[1] or {}
    assert "User-Agent" in retry_headers
    assert "Chrome" in retry_headers["User-Agent"]


def test_non_403_error_is_unchanged(monkeypatch):
    calls = []

    def fake_request(url, method="GET", **kwargs):
        calls.append(kwargs.get("headers"))
        raise http_client.HTTPError("HTTP 404", status_code=404)

    monkeypatch.setattr(http_client, "request", fake_request)

    result = resolver.resolve_from_page("Acme", "https://careers.acme.com")

    assert result["provider"] == UNKNOWN
    assert result["url"] == "https://careers.acme.com"
    # No escalation / second attempt on a non-403 failure.
    assert len(calls) == 1
