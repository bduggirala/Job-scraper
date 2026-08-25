"""SAP SuccessFactors collector.

SuccessFactors' OData job API requires tenant credentials, so public career
sites are only reachable through their server-rendered search page::

    https://{host}/search/?q=&sortColumn=referencedate&sortDirection=desc

This collector parses JSON-LD when present (real dates) and falls back to the
standard ``/job/`` anchor markup. Client-rendered tenants raise
CollectorUnavailable and go to Playwright.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

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
from ats.detector import SUCCESSFACTORS
from ats.html_utils import extract_job_links, iter_jsonld_jobs, jsonld_location

PAGE_STEP = 25


class SuccessFactorsCollector(ATSCollector):
    provider = SUCCESSFACTORS

    def _search_url(self) -> str:
        if not self.url:
            raise CollectorUnavailable("No SuccessFactors URL available")
        parts = urlsplit(self.url)
        if not parts.netloc:
            raise CollectorUnavailable("Unparseable SuccessFactors URL")
        return urlunsplit((parts.scheme or "https", parts.netloc, "/search/", "", ""))

    def collect(self) -> CollectionResult:
        search_url = self._search_url()
        records: list[dict | None] = []
        seen: set[str] = set()
        page = -1
        complete = True
        stop_reason = STOP_EXHAUSTED

        while len(records) < self.max_jobs:
            page += 1
            params = {
                "q": "",
                "sortColumn": "referencedate",
                "sortDirection": "desc",
                "startrow": page * PAGE_STEP,
            }
            try:
                html_text = http_client.get_text(
                    search_url, params=params, headers={"Accept": "text/html"}
                )
            except Exception as exc:
                if page == 0:
                    raise CollectorUnavailable(f"SuccessFactors search unavailable: {exc}") from exc
                self.log.warning("%s: SuccessFactors page %s failed (%s); marking incomplete",
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
                        date_posted=link.get("date_posted"),
                        job_url=link["job_url"],
                    )
                    for link in extract_job_links(html_text, search_url, selector='a[href*="/job/"]')
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
            raise CollectorUnavailable("SuccessFactors search returned zero jobs")
        jobs = self.finalize(records)
        if not complete:
            self.log.warning("%s: SuccessFactors scrape INCOMPLETE (%s) - collected %s",
                             self.company, stop_reason, len(jobs))
        return CollectionResult(jobs=jobs, complete=complete,
                                pages_fetched=page + 1, stop_reason=stop_reason)
