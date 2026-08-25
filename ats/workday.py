"""Workday collector - CXS jobs endpoint.

Workday exposes a stable, unauthenticated JSON search endpoint behind every
public career site::

    POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

The response carries ``total`` and a ``jobPostings`` array. ``limit`` is capped
at 20 server-side, so results are paged with ``offset``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import http_client
from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_PAGE_FAILED,
    STOP_TOTAL_REACHED,
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
from ats.detector import WORKDAY

PAGE_SIZE = 20


class WorkdayCollector(ATSCollector):
    provider = WORKDAY

    def _endpoint(self) -> str:
        if not self.host or not self.tenant or not self.site:
            raise CollectorUnavailable(
                f"Incomplete Workday coordinates (host={self.host}, "
                f"tenant={self.tenant}, site={self.site})"
            )
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    def _base_site_url(self) -> str:
        """Public site root used to turn externalPath into an absolute URL."""
        parts = urlsplit(self.url or f"https://{self.host}")
        segments = [s for s in parts.path.split("/") if s]

        locale = "en-US"
        for segment in segments:
            if re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", segment):
                locale = segment
                break
        return f"https://{self.host}/{locale}/{self.site}"

    def _job_url(self, external_path: str | None) -> str | None:
        if not external_path:
            return None
        if external_path.startswith("http"):
            return external_path
        return urljoin(self._base_site_url().rstrip("/") + "/", external_path.lstrip("/"))

    @staticmethod
    def _extract_posted(posting: dict[str, Any]) -> Any:
        """Workday puts the date in postedOn, or in a bulletFields entry."""
        for key in ("postedOn", "startDate", "postedOnDate"):
            value = posting.get(key)
            if value:
                return value

        for bullet in posting.get("bulletFields") or []:
            if isinstance(bullet, str) and re.search(r"posted|ago|today|yesterday", bullet, re.I):
                return bullet
        return None

    def collect(self) -> CollectionResult:
        endpoint = self._endpoint()
        records: list[dict | None] = []
        offset = 0
        pages = 0
        total: int | None = None
        stop_reason = STOP_EXHAUSTED
        complete = True

        while len(records) < self.max_jobs:
            payload = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                # Newest-first. Without an explicit sort Workday returns rows in
                # an unspecified order, so a truncated walk kept an arbitrary
                # slice of the tenant - useless against a freshness window, and
                # unstable between runs (which churned the removal sync).
                "searchText": "",
                "sortBy": "POSTING_DATES_DESC",
            }
            try:
                data = http_client.post_json(
                    endpoint,
                    payload,
                    headers={"Accept": "application/json", "Referer": self._base_site_url()},
                )
            except Exception as exc:
                if pages == 0:
                    raise CollectorUnavailable(f"Workday CXS unavailable: {exc}") from exc
                # Earlier pages are real and worth keeping, but this walk is
                # short: say so, or the pipeline will delete the rows we never
                # got to as though the postings had closed.
                self.log.warning(
                    "%s: Workday page %s failed (%s); keeping %s jobs and marking "
                    "the scrape incomplete", self.company, pages, exc, len(records),
                )
                complete, stop_reason = False, STOP_PAGE_FAILED
                break

            if not isinstance(data, dict):
                raise CollectorUnavailable("Workday CXS returned a non-object response")

            postings = data.get("jobPostings") or []
            if total is None:
                total = data.get("total")

            # An empty page means the provider is done, whatever `total` said.
            if not postings:
                stop_reason = STOP_EXHAUSTED
                break

            pages += 1
            for posting in postings:
                if not isinstance(posting, dict):
                    continue
                records.append(
                    self.record(
                        title=posting.get("title"),
                        location=posting.get("locationsText") or posting.get("locations"),
                        date_posted=self._extract_posted(posting),
                        job_url=self._job_url(posting.get("externalPath")),
                        employment_type=posting.get("timeType"),
                        description=posting.get("jobDescription"),
                    )
                )

            offset += PAGE_SIZE
            if total is not None and offset >= int(total):
                stop_reason = STOP_TOTAL_REACHED
                break
        else:
            # Budget tripped with rows still outstanding.
            complete, stop_reason = False, STOP_BUDGET

        if not records:
            raise CollectorUnavailable("Workday CXS returned zero postings")

        jobs = self.finalize(records)
        if not complete:
            self.log.warning(
                "%s: Workday scrape INCOMPLETE (%s) - collected %s of %s reported",
                self.company, stop_reason, len(jobs), total,
            )
        return CollectionResult(
            jobs=jobs, complete=complete, pages_fetched=pages,
            reported_total=total, stop_reason=stop_reason,
        )
