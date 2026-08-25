"""Paylocity Recruiting collector.

Paylocity job boards live at::

    https://recruiting.paylocity.com/recruiting/jobs/All/{companyGuid}/{slug}

There is an internal JSON endpoint backing that page which this collector
tries first; when it is absent or reshaped, the collector falls back to
parsing the server-rendered list (and any JSON-LD), and finally raises
CollectorUnavailable so Playwright takes over.
"""

from __future__ import annotations

import http_client
from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_PAGE_FAILED,
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
from ats.detector import PAYLOCITY
from ats.html_utils import extract_job_links, iter_jsonld_jobs, jsonld_location

BASE = "https://recruiting.paylocity.com"
PAGE_SIZE = 200


class PaylocityCollector(ATSCollector):
    provider = PAYLOCITY

    def _guid(self) -> str:
        guid = self.identifier or self.tenant
        if not guid:
            raise CollectorUnavailable("No Paylocity company GUID in URL")
        return str(guid)

    def _try_api(self, guid: str) -> CollectionResult:
        endpoint = f"{BASE}/recruiting/v2/api/jobs"
        records: list[dict | None] = []
        page = 0
        complete = True
        stop_reason = STOP_EXHAUSTED

        while len(records) < self.max_jobs:
            page += 1
            try:
                data = http_client.get_json(
                    endpoint,
                    params={"companyId": guid, "pageSize": PAGE_SIZE, "pageNumber": page},
                    headers={"Accept": "application/json"},
                )
            except Exception as exc:
                if page == 1:
                    raise CollectorUnavailable(
                        f"Paylocity API unavailable: {exc}"
                    ) from exc
                self.log.warning(
                    "%s: Paylocity page %s failed (%s); marking incomplete",
                    self.company, page, exc,
                )
                complete, stop_reason = False, STOP_PAGE_FAILED
                break

            jobs = (
                data if isinstance(data, list)
                else (data or {}).get("jobs") or (data or {}).get("items")
            )
            if not isinstance(jobs, list):
                if page == 1:
                    raise CollectorUnavailable("Paylocity API returned no usable job list")
                break
            if not jobs:
                break

            for job in jobs:
                if not isinstance(job, dict):
                    continue
                job_id = job.get("jobId") or job.get("id")
                records.append(
                    self.record(
                        title=job.get("title") or job.get("jobTitle"),
                        location=job.get("location") or job.get("locationName"),
                        date_posted=job.get("publishedDate") or job.get("postedDate"),
                        job_url=job.get("url")
                        or (f"{BASE}/recruiting/jobs/Details/{job_id}" if job_id else None),
                        employment_type=job.get("employmentType") or job.get("jobType"),
                        description=job.get("description"),
                    )
                )

            # A short page is the last page: the endpoint has no total to
            # reconcile against, so page length is the only end marker.
            if len(jobs) < PAGE_SIZE:
                break
        else:
            complete, stop_reason = False, STOP_BUDGET

        if not any(records):
            raise CollectorUnavailable("Paylocity API rows lacked title/url")
        return CollectionResult(
            jobs=self.finalize(records), complete=complete,
            pages_fetched=page, stop_reason=stop_reason,
        )

    def _try_html(self, guid: str) -> CollectionResult:
        list_url = self.url or f"{BASE}/recruiting/jobs/All/{guid}"
        html_text = http_client.get_text(list_url, headers={"Accept": "text/html"})

        records = [
            self.record(
                title=node.get("title"),
                location=jsonld_location(node),
                date_posted=node.get("datePosted"),
                job_url=node.get("url") or node.get("@id"),
                employment_type=node.get("employmentType"),
                description=node.get("description"),
            )
            for node in iter_jsonld_jobs(html_text)
        ]

        if not any(records):
            records = [
                self.record(
                    title=link["title"],
                    location=link.get("location"),
                    job_url=link["job_url"],
                )
                for link in extract_job_links(html_text, list_url)
            ]

        if not any(records):
            raise CollectorUnavailable("Paylocity HTML contained no job rows")
        # The server-rendered list is a single page with no pagination control,
        # so what it shows is all it will show.
        return CollectionResult(jobs=self.finalize(records), stop_reason=STOP_EXHAUSTED)

    def collect(self) -> CollectionResult:
        guid = self._guid()
        try:
            return self._try_api(guid)
        except Exception as api_error:
            self.log.debug("%s: Paylocity API path failed (%s); trying HTML", self.company, api_error)
            try:
                return self._try_html(guid)
            except Exception as html_error:
                raise CollectorUnavailable(
                    f"Paylocity API and HTML both failed: {api_error} / {html_error}"
                ) from html_error
