"""Amazon.jobs collector - Amazon's own careers site with a public JSON API.

Amazon does not run on a third-party ATS; it operates a custom careers site at
``www.amazon.jobs`` backed by a clean, unauthenticated JSON search endpoint::

    GET https://www.amazon.jobs/en/search.json?result_limit=100&offset=0&sort=recent

which answers ``{"error": null, "hits": <int>, "jobs": [{...}]}``. Each job
carries a ``title``, a site-relative ``job_path`` (``/en/jobs/{id}/{slug}`` whose
absolute URL is ``https://www.amazon.jobs`` + ``job_path``), a
``normalized_location`` (with ``city``/``state``/``country_code`` as a fallback),
a human ``posted_date`` (``"August 21, 2026"`` - :func:`normalize.parse_date`
handles it via ``dateutil``) and a ``job_schedule_type`` employment hint.

Because the host is Amazon's own, :func:`ats.detector.detect_ats` recognises it
by the ``amazon.jobs`` host pattern rather than a vendor domain; the tenant is
simply ``"amazon"``.

Pagination walks ``offset`` in ``RESULT_LIMIT`` steps until ``offset >= hits`` or
the bounded :data:`MAX_PAGES` guard trips, so a tenant that misreports ``hits``
cannot spin forever. No role or location filtering is applied here - downstream
filters own that; the collector fetches every posting the endpoint will serve.
"""

from __future__ import annotations

from typing import Any

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from normalize import join_location

try:  # pragma: no cover - falls back until AMAZON lands in the detector
    from ats.detector import AMAZON
except ImportError:  # detector snippet not yet applied
    AMAZON = "amazon"

# The search endpoint is served from Amazon's own host regardless of the tenant
# URL recorded for the company, so the collector always targets it directly.
SEARCH_URL = "https://www.amazon.jobs/en/search.json"
JOB_BASE_URL = "https://www.amazon.jobs"

# 100 jobs/page keeps the request count reasonable while staying well within the
# limit the endpoint accepts. ``hits`` currently caps at 10,000, so ~100 pages
# covers the full public window; MAX_PAGES bounds a misreported total.
RESULT_LIMIT = 100
MAX_PAGES = 200


class AmazonJobsCollector(ATSCollector):
    """Collector for Amazon's ``www.amazon.jobs`` public search API."""

    provider = AMAZON

    def _fetch_page(self, offset: int) -> dict[str, Any]:
        """Fetch one ``search.json`` page at ``offset``.

        Returns the parsed JSON object; raising is left to the caller so
        pagination can decide whether a failure is fatal (first page) or a
        tolerable early stop (a later page).
        """
        params = {
            "result_limit": RESULT_LIMIT,
            "offset": offset,
            "sort": "recent",
        }
        data = http_client.get_json(
            SEARCH_URL,
            params=params,
            headers={"Accept": "application/json, text/javascript, */*"},
        )
        if not isinstance(data, dict):
            raise CollectorUnavailable("Amazon search.json did not return an object")
        return data

    @staticmethod
    def _location(job: dict[str, Any]) -> str | None:
        """Prefer the pre-normalized location, else assemble city/state/country."""
        normalized = job.get("normalized_location")
        if normalized:
            return normalized
        return join_location(job.get("city"), job.get("state"), job.get("country_code"))

    def _parse_jobs(self, jobs: list[dict[str, Any]]) -> list[dict]:
        """Turn a page's ``jobs`` array into normalized records."""
        rows: list[dict] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_path = job.get("job_path") or ""
            if job_path.startswith("/"):
                job_url = f"{JOB_BASE_URL}{job_path}"
            else:
                job_url = job_path
            rows.append(
                self.record(
                    title=job.get("title"),
                    location=self._location(job),
                    date_posted=job.get("posted_date"),
                    job_url=job_url,
                    employment_type=job.get("job_schedule_type"),
                )
            )
        return rows

    def collect(self) -> list[dict]:
        """Paginate ``search.json`` and return every posting as a record.

        Raises:
            CollectorUnavailable: the first request fails or reports zero hits,
                signalling the router to fall back to Playwright.
        """
        try:
            first = self._fetch_page(0)
        except Exception as exc:
            raise CollectorUnavailable(
                f"Amazon search.json unavailable: {exc}"
            ) from exc

        hits = int(first.get("hits") or 0)
        if hits <= 0 and not first.get("jobs"):
            raise CollectorUnavailable("Amazon search.json returned zero jobs")

        records: list[dict | None] = []
        seen: set[str] = set()

        def absorb(jobs: list[dict[str, Any]]) -> None:
            for row in self._parse_jobs(jobs or []):
                if not row:
                    continue
                url = row["job_url"]
                if url in seen:
                    continue
                seen.add(url)
                records.append(row)

        absorb(first.get("jobs") or [])

        offset = RESULT_LIMIT
        for _ in range(1, MAX_PAGES):
            if offset >= hits:
                break
            try:
                page = self._fetch_page(offset)
            except Exception:
                # A later page failing is tolerated: keep what we already have
                # rather than discarding a large, valid partial harvest.
                break
            page_jobs = page.get("jobs") or []
            if not page_jobs:
                break
            absorb(page_jobs)
            offset += RESULT_LIMIT

        if not records:
            raise CollectorUnavailable("Amazon search.json yielded no parseable jobs")
        return self.finalize(records)
