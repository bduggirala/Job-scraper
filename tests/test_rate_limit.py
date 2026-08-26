"""Per-host rate limiting and bounded reads in the shared HTTP client.

Raising the collector ceiling from 25 pages to 10,000 jobs multiplied the
requests a single company can make - a large Workday tenant went from 25
requests to as many as 500. Ten HTTP workers running that flat out against one
vendor host is neither polite nor safe: it is the usual way to earn a block.

These tests pin three properties: requests to one host are paced, different
hosts do not queue behind each other, and a response body cannot be read
without bound.
"""

import time

import pytest

import http_client
from http_client import HostRateLimiter


def test_requests_to_one_host_are_paced():
    limiter = HostRateLimiter(rate_per_second=20.0)   # 50ms apart

    start = time.monotonic()
    for _ in range(4):
        limiter.acquire("careers.example.com")
    elapsed = time.monotonic() - start

    # First is free, the next three wait ~50ms each.
    assert elapsed >= 0.13, f"no pacing applied (took {elapsed:.3f}s)"


def test_separate_hosts_do_not_queue_behind_each_other():
    """A slow vendor host must not throttle every other company's scrape."""
    limiter = HostRateLimiter(rate_per_second=2.0)    # 500ms apart per host

    start = time.monotonic()
    for host in ("a.example.com", "b.example.com", "c.example.com", "d.example.com"):
        limiter.acquire(host)
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, f"hosts shared one budget (took {elapsed:.3f}s)"


def test_the_limiter_is_keyed_on_host_not_full_url():
    limiter = HostRateLimiter(rate_per_second=20.0)

    start = time.monotonic()
    limiter.acquire("careers.example.com")
    limiter.acquire("careers.example.com")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.04


def test_a_disabled_limiter_does_not_pace():
    limiter = HostRateLimiter(rate_per_second=0)

    start = time.monotonic()
    for _ in range(10):
        limiter.acquire("careers.example.com")
    assert time.monotonic() - start < 0.05


# --- bounded reads ---------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in exposing the streaming surface get_text uses."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.encoding = "utf-8"
        self.headers = {"Content-Type": "text/html"}
        self.status_code = 200
        self.url = "https://careers.example.com/jobs"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield from self._chunks

    def close(self):
        self.closed = True


def test_a_body_is_truncated_at_the_configured_ceiling(monkeypatch):
    """An unbounded read across 10 workers is a memory-exhaustion risk."""
    huge = [b"x" * 100_000 for _ in range(200)]        # 20 MB
    monkeypatch.setattr(http_client, "request", lambda *a, **kw: _FakeResponse(huge))

    text = http_client.get_text("https://careers.example.com/jobs", max_bytes=500_000)

    assert len(text) <= 500_000


def test_a_small_body_is_returned_intact(monkeypatch):
    monkeypatch.setattr(
        http_client, "request",
        lambda *a, **kw: _FakeResponse([b"<html>Data Engineer</html>"]),
    )

    assert http_client.get_text("https://careers.example.com/jobs") == (
        "<html>Data Engineer</html>"
    )


# --- backoff jitter --------------------------------------------------------

def test_retry_backoff_carries_jitter():
    """Ten workers retrying in lockstep is a self-inflicted thundering herd."""
    waits = {http_client.retry_wait_seconds(attempt=2) for _ in range(40)}
    assert len(waits) > 1, "backoff is deterministic; no jitter applied"
