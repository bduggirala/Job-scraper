"""Shared HTTP client with timeouts, retries, exponential backoff and
explicit rate-limit (HTTP 429) handling.

A single retry mechanism is used deliberately: ``tenacity`` wraps the request
call, and urllib3's transport-level retries are disabled so the two do not
compound into retries^2 attempts.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
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
        wait=wait_exponential(multiplier=float(backoff_factor), min=1, max=60),
        retry=retry_if_exception_type((RetryableHTTPError, requests.RequestException)),
        before_sleep=_log_retry,
        reraise=True,
    )
    def _do_request() -> requests.Response:
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


def get_text(url: str, **kwargs: Any) -> str:
    """GET a URL and return the decoded body text."""
    return request(url, method="GET", **kwargs).text
