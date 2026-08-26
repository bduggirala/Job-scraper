"""Shared HTTP client with timeouts, retries, exponential backoff and
explicit rate-limit (HTTP 429) handling.

A single retry mechanism is used deliberately: ``tenacity`` wraps the request
call, and urllib3's transport-level retries are disabled so the two do not
compound into retries^2 attempts.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from logger import get_logger
from settings import load_settings

log = get_logger("http")

# Status codes worth retrying: rate limiting plus transient server failures.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RetryableHTTPError(Exception):
    """Raised for responses that should trigger a tenacity retry."""

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class HTTPError(Exception):
    """Non-retryable HTTP failure (4xx other than rate limiting)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


_thread_local = threading.local()


class HostRateLimiter:
    """Paces outbound requests per hostname.

    One shared instance across every worker thread, because politeness is a
    property of the *host* being called, not of the thread calling it. Ten HTTP
    workers all scraping different companies on ``myworkdayjobs.com`` are ten
    concurrent callers of one vendor.

    Keyed on host so a slow vendor never throttles unrelated companies: each
    host gets its own next-allowed timestamp.

    A rate of 0 disables pacing entirely.
    """

    def __init__(self, rate_per_second: float):
        self.min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._next_free: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, host: str) -> None:
        """Block until this host may be called again."""
        if not self.min_interval or not host:
            return

        with self._lock:
            now = time.monotonic()
            earliest = self._next_free.get(host, 0.0)
            wait = max(0.0, earliest - now)
            # Reserve this slot before releasing the lock, so concurrent
            # callers queue behind each other instead of all sleeping the
            # same interval and then firing together.
            self._next_free[host] = max(now, earliest) + self.min_interval

        if wait:
            time.sleep(wait)


_limiter: HostRateLimiter | None = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> HostRateLimiter:
    """The process-wide per-host limiter, built from settings on first use."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                cfg = load_settings()
                _limiter = HostRateLimiter(
                    float(cfg.get("requests.per_host_rate_per_second", 3.0))
                )
    return _limiter


def retry_wait_seconds(attempt: int, backoff_factor: float = 2.0) -> float:
    """Exponential backoff with full jitter, capped at 60s.

    Jitter matters here specifically because ten workers share one retry
    schedule: without it they back off in lockstep and retry simultaneously,
    which is the thundering herd that turns a transient 503 into a sustained
    one.
    """
    ceiling = min(60.0, backoff_factor * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, max(0.001, ceiling))


def _build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    # max_retries=0: tenacity owns retry policy (see module docstring).
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_session() -> requests.Session:
    """Return a per-thread requests Session (Sessions are not thread-safe)."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        cfg = load_settings()
        session = _build_session(cfg.get("requests.user_agent", "Mozilla/5.0"))
        _thread_local.session = session
    return session


def _parse_retry_after(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # HTTP-date form; fall back to the caller's backoff schedule.
        return None


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    url = state.kwargs.get("url") or (state.args[0] if state.args else "?")
    log.debug("Retry %s for %s after %s", state.attempt_number, url, exc)


def request(
    url: str,
    method: str = "GET",
    *,
    timeout: float | None = None,
    retries: int | None = None,
    backoff_factor: float | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Perform an HTTP request with retry/backoff and rate-limit handling.

    Raises:
        RetryableHTTPError: retries exhausted on a transient/429 response.
        HTTPError: non-retryable 4xx response.
        requests.RequestException: connection-level failure after retries.
    """
    cfg = load_settings()
    timeout = timeout if timeout is not None else cfg.get("requests.timeout_seconds", 30)
    retries = retries if retries is not None else cfg.get("requests.retries", 3)
    backoff_factor = (
        backoff_factor if backoff_factor is not None else cfg.get("requests.backoff_factor", 2)
    )

    @retry(
        stop=stop_after_attempt(max(1, int(retries))),
        # Jittered: ten workers share this schedule, and backing off in
        # lockstep turns a transient 503 into a sustained one.
        wait=wait_exponential_jitter(initial=1, max=60, exp_base=2,
                                     jitter=float(backoff_factor)),
        retry=retry_if_exception_type((RetryableHTTPError, requests.RequestException)),
        before_sleep=_log_retry,
        reraise=True,
    )
    def _do_request() -> requests.Response:
        # Politeness is per-host, and applies to every attempt including
        # retries - a retry storm is exactly when pacing matters most.
        get_rate_limiter().acquire(urlsplit(url).netloc.split("@")[-1].split(":")[0])

        session = get_session()
        response = session.request(method, url, timeout=timeout, **kwargs)

        if response.status_code in RETRYABLE_STATUS:
            retry_after = _parse_retry_after(response)
            if response.status_code == 429 and retry_after:
                # Honour the server's own pacing before tenacity's backoff.
                log.warning("Rate limited by %s; honouring Retry-After=%ss", url, retry_after)
                time.sleep(min(retry_after, 120))
            raise RetryableHTTPError(
                f"HTTP {response.status_code} from {url}",
                status_code=response.status_code,
                retry_after=retry_after,
            )

        if 400 <= response.status_code < 500:
            raise HTTPError(f"HTTP {response.status_code} from {url}", status_code=response.status_code)

        response.raise_for_status()
        return response

    return _do_request()


def get_json(url: str, **kwargs: Any) -> Any:
    """GET a URL and parse the body as JSON."""
    response = request(url, method="GET", **kwargs)
    return response.json()


def post_json(url: str, payload: Any, **kwargs: Any) -> Any:
    """POST a JSON payload and parse the JSON response."""
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Content-Type", "application/json")
    response = request(url, method="POST", json=payload, headers=headers, **kwargs)
    return response.json()


#: Default ceiling on a response body read into memory. Career pages are
#: routinely 1-2 MB and occasionally larger; 8 MB is generous for real content
#: while still bounding a hostile or misconfigured endpoint across 10 workers.
MAX_RESPONSE_BYTES = 8_000_000


def get_text(url: str, *, max_bytes: int | None = None, **kwargs: Any) -> str:
    """GET a URL and return the decoded body text, bounded in size.

    Reads incrementally and stops at ``max_bytes``. An unbounded ``.text`` on
    ten concurrent workers is a memory-exhaustion risk, and no career page
    worth parsing needs more than a few megabytes - the parsers only ever look
    at markup near the top of the document anyway.
    """
    limit = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    kwargs.setdefault("stream", True)
    response = request(url, method="GET", **kwargs)

    try:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                log.debug("Truncating %s at %s bytes", url, limit)
                break
        body = b"".join(chunks)[:limit]
    finally:
        response.close()

    return body.decode(response.encoding or "utf-8", errors="replace")
