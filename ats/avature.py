"""Avature collector - SearchJobs pages.

Avature career portals are server-rendered at::

    https://{tenant}.avature.net/careers/SearchJobs/?jobRecordsPerPage=100&jobOffset=0

Some tenants also honour ``?format=json``; that is tried first because it
gives structured rows, with the HTML list as the fallback.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import http_client
from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_NO_NEW_ROWS,
    STOP_PAGE_FAILED,
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
from ats.detector import AVATURE
from ats.html_utils import extract_job_links, iter_jsonld_jobs, jsonld_location

PAGE_SIZE = 100


class AvatureCollector(ATSCollector):
    provider = AVATURE

    def _host(self) -> str:
        if self.host:
            return self.host
        if self.url:
            return urlsplit(self.url).netloc
        raise CollectorUnavailable("No Avature host available")

    def collect(self) -> CollectionResult:
        host = self._host()
        search_url = f"https://{host}/careers/SearchJobs/"

        records: list[dict | None] = []
        seen: set[str] = set()
        page = -1
        complete = True
        stop_reason = STOP_EXHAUSTED

        while len(records) < self.max_jobs:
            page += 1
            params = {
                "jobRecordsPerPage": PAGE_SIZE,
                "jobOffset": page * PAGE_SIZE,
            }
            try:
                html_text = http_client.get_text(
                    search_url, params=params, headers={"Accept": "text/html"}
                )
            except Exception as exc:
                if page == 0:
                    raise CollectorUnavailable(f"Avature search unavailable: {exc}") from exc
                self.log.warning("%s: Avature page %s failed (%s); marking incomplete",
                                 self.company, page, exc)
                complete, stop_reason = False, STOP_PAGE_FAILED
                break

            page_records = [
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

            if not any(page_records):
                page_records = [
                    self.record(
                        title=link["title"],
                        location=link.get("location"),
                        job_url=link["job_url"],
                    )
                    for link in extract_job_links(
                        html_text, search_url, selector='a[href*="JobDetail"]'
                    )
                ]

            fresh = [r for r in page_records if r and r["job_url"] not in seen]
            if not fresh:
                stop_reason = STOP_NO_NEW_ROWS
                break
            for record in fresh:
                seen.add(record["job_url"])
            records.extend(fresh)
        else:
            complete, stop_reason = False, STOP_BUDGET

        if not records:
            raise CollectorUnavailable("Avature search returned zero jobs")
        jobs = self.finalize(records)
        if not complete:
            self.log.warning("%s: Avature scrape INCOMPLETE (%s) - collected %s",
                             self.company, stop_reason, len(jobs))
        return CollectionResult(jobs=jobs, complete=complete,
                                pages_fetched=page + 1, stop_reason=stop_reason)
