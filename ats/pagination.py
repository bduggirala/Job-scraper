"""One pagination walk, shared by every collector that has pages.

Fourteen collectors used to implement this loop by hand, which is precisely why
the same defect turned up in eight of them at once: a page beyond the first
failed, the loop ``break``-ed, and the partial harvest was returned as though
it were the whole job list.

Centralising it also buys two things none of the hand-written versions had:

* **per-page retry.** A transient failure on page 12 of 25 used to end the walk
  and mark the company incomplete, which suppresses removal sync until the next
  clean run. Most such failures succeed on a second attempt, so retrying turns
  a truncated scrape back into a complete one.
* **repeated-page detection.** A tenant that ignores its own paging parameter
  serves page 1 forever. The old loops relied on "no new rows" to notice, which
  works, but only after parsing and de-duplicating every repeat; comparing a
  hash of the raw page stops on the first repeat instead.

Providers index pages three different ways (byte offset, 0-based index, 1-based
number), so :class:`PageRequest` carries all three and each collector reads the
one its API wants.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_NO_NEW_ROWS,
    STOP_PAGE_CEILING,
    STOP_PAGE_FAILED,
    STOP_REPEATED_PAGE,
    STOP_SHORT_OF_TOTAL,
    STOP_TOTAL_REACHED,
    TOTAL_RECONCILIATION_TOLERANCE,
)
from logger import get_logger

log = get_logger("ats.pagination")

#: Hard ceiling on pages, independent of the job budget. Guards a provider that
#: serves one row per page forever without ever repeating content.
MAX_PAGES = 500


@dataclass(frozen=True)
class PageRequest:
    """Coordinates for one page, in every form a provider might want."""

    #: Row offset: ``page_index * page_size``. Workday, SmartRecruiters, ...
    offset: int
    #: Zero-based page counter. iCIMS ``pr``, ...
    page_index: int
    #: One-based page counter. Jibe, Cornerstone, Paylocity, Radancy, ...
    page_number: int
    #: Rows requested per page.
    page_size: int


@dataclass
class PageWalk:
    """Outcome of a paginated walk."""

    items: list[Any] = field(default_factory=list)
    complete: bool = True
    pages_fetched: int = 0
    reported_total: int | None = None
    stop_reason: str = STOP_EXHAUSTED


#: ``fetch(request) -> (rows, reported_total_or_None)``
FetchPage = Callable[[PageRequest], "tuple[Iterable[Any], int | None]"]


def _reconcile(items: list[Any], total: int | None, pages: int, reason: str) -> PageWalk:
    """Finish a walk, checking what we collected against what was promised.

    A provider that reports a total and then stops serving rows before reaching
    it has contradicted itself, and the difference is postings we never saw.
    Reporting that as a complete scrape is what lets removal sync delete them:
    it is the same class of error as a failed page, just quieter, because
    nothing raised.

    A small shortfall is tolerated - see
    :data:`ats.base.TOTAL_RECONCILIATION_TOLERANCE`.

    Only ``STOP_EXHAUSTED`` is rewritten. That reason is a *claim* - "the
    provider served everything it had" - and a reported total contradicting it
    makes the claim false. The other reasons describe an observed event
    (a page repeated, a page contributed nothing new); those stay true and
    remain far more useful for diagnosis, so completeness alone is flipped.
    """
    walk = PageWalk(items, True, pages, total, reason)
    if total is None or total <= 0:
        return walk

    missing = total - len(items)
    if missing <= max(1, int(total * TOTAL_RECONCILIATION_TOLERANCE)):
        return walk

    log.warning(
        "pagination stopped (%s) with %s of %s row(s) the provider reported; "
        "marking the scrape incomplete so removal sync is skipped",
        reason, len(items), total,
    )
    walk.complete = False
    if reason == STOP_EXHAUSTED:
        walk.stop_reason = STOP_SHORT_OF_TOTAL
    return walk


def _page_fingerprint(rows: list[Any]) -> str:
    """Stable hash of a page's content, for spotting an exact repeat."""
    try:
        blob = json.dumps(rows, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        blob = repr(rows)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()


def paginate(
    fetch: FetchPage,
    *,
    page_size: int,
    max_jobs: int,
    key: Callable[[Any], Any] | None = None,
    page_retries: int = 2,
    retry_backoff_seconds: float = 1.0,
    label: str = "",
) -> PageWalk:
    """Walk a provider's pages until it runs out, or a bound is reached.

    Args:
        fetch: called with a :class:`PageRequest`; returns
            ``(rows, reported_total)``. ``reported_total`` may be None on any
            page - the first non-None value is kept.
        page_size: rows requested per page.
        max_jobs: ceiling on collected rows. Tripping it makes the walk
            incomplete, which is honest rather than silent.
        key: identity function for de-duplication across pages. When given,
            rows already seen are dropped and a page contributing nothing new
            ends the walk.
        page_retries: attempts per page. A first-page failure always
            propagates so the collector can raise ``CollectorUnavailable`` and
            let the router fall back; later pages are retried and then
            tolerated as an incomplete walk.

    Returns:
        A :class:`PageWalk`. ``complete`` is False only when rows are known to
        be missing - a failed page or a tripped budget.
    """
    items: list[Any] = []
    seen_keys: set[Any] = set()
    seen_pages: set[str] = set()
    total: int | None = None
    pages = 0
    index = 0

    while len(items) < max_jobs and index < MAX_PAGES:
        request = PageRequest(
            offset=index * page_size,
            page_index=index,
            page_number=index + 1,
            page_size=page_size,
        )

        rows, page_total, failure = _fetch_with_retry(
            fetch, request, page_retries, retry_backoff_seconds, label,
        )
        if failure is not None:
            if pages == 0:
                raise failure          # let the collector fall back entirely
            log.warning("%s: page %s failed after %s attempt(s) (%s); "
                        "keeping %s row(s) and marking incomplete",
                        label or "pagination", request.page_number,
                        page_retries, failure, len(items))
            return PageWalk(items, False, pages, total, STOP_PAGE_FAILED)

        if total is None and page_total is not None:
            total = int(page_total)

        rows = list(rows or [])
        if not rows:
            return _reconcile(items, total, pages, STOP_EXHAUSTED)

        fingerprint = _page_fingerprint(rows)
        if fingerprint in seen_pages:
            log.debug("%s: page %s repeated an earlier page; stopping",
                      label or "pagination", request.page_number)
            return _reconcile(items, total, pages, STOP_REPEATED_PAGE)
        seen_pages.add(fingerprint)

        pages += 1
        fresh = rows
        if key is not None:
            fresh = []
            for row in rows:
                identity = key(row)
                if identity in seen_keys:
                    continue
                seen_keys.add(identity)
                fresh.append(row)
            if not fresh:
                return _reconcile(items, total, pages, STOP_NO_NEW_ROWS)

        items.extend(fresh)

        if total is not None and len(items) >= total:
            return PageWalk(items, True, pages, total, STOP_TOTAL_REACHED)

        # A page shorter than requested usually means the provider is done -
        # but only trust that when nothing contradicts it. When a total was
        # reported and we are still short of it, a short page is ambiguous
        # (an over-reported total, or a provider quirk), so spend one more
        # request: the empty page that follows ends the walk honestly, while
        # stopping here would report 2 of 200 rows as a complete scrape.
        if len(rows) < page_size and total is None:
            return _reconcile(items, total, pages, STOP_EXHAUSTED)

        index += 1

    # Which bound actually ended the loop matters. Deriving the reason from the
    # job budget alone reported a walk that ran out of *pages* as "exhausted",
    # which reads as complete - and on a ten-rows-per-request provider the page
    # ceiling is only 5,000 jobs however high max_jobs is set.
    if len(items) >= max_jobs:
        log.warning("%s: stopped at the %s-job budget with %s reported",
                    label or "pagination", max_jobs, total)
        return PageWalk(items[:max_jobs], False, pages, total, STOP_BUDGET)

    if index >= MAX_PAGES:
        log.warning("%s: stopped at the %s-page ceiling with %s row(s) "
                    "collected and %s reported",
                    label or "pagination", MAX_PAGES, len(items), total)
        return PageWalk(items[:max_jobs], False, pages, total, STOP_PAGE_CEILING)

    return _reconcile(items[:max_jobs], total, pages, STOP_EXHAUSTED)


def _fetch_with_retry(
    fetch: FetchPage,
    request: PageRequest,
    attempts: int,
    backoff: float,
    label: str,
) -> tuple[Iterable[Any], int | None, Exception | None]:
    """Fetch one page, retrying transient failures.

    Returns ``(rows, total, failure)`` - ``failure`` is the last exception when
    every attempt failed, and None otherwise.
    """
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            rows, total = fetch(request)
            return rows, total, None
        except Exception as exc:
            last = exc
            if attempt < attempts - 1:
                delay = backoff * (attempt + 1)
                log.debug("%s: page %s attempt %s failed (%s); retrying in %.1fs",
                          label or "pagination", request.page_number,
                          attempt + 1, exc, delay)
                time.sleep(delay)
    return [], None, last
