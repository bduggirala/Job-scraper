"""iCIMS collector - server-rendered search pages.

iCIMS has no public JSON API. The public job list is server-rendered at::

    https://careers-{tenant}.icims.com/jobs/search?pr={page}&in_iframe=1

so this collector parses HTML (and any JSON-LD the page emits, which is more
reliable when present). Pagination walks ``pr`` until a page yields nothing new.
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
from ats.detector import ICIMS
from ats.html_utils import extract_job_links, iter_jsonld_jobs, jsonld_location

# Rows per search page, used to translate the job budget into a page count.
ROWS_PER_PAGE = 20


class ICIMSCollector(ATSCollector):
    provider = ICIMS

    def _base_host(self) -> str:
        if self.host:
            return self.host
        if self.url:
            return urlsplit(self.url).netloc
        raise CollectorUnavailable("No iCIMS host available")

    def collect(self) -> CollectionResult:
        host = self._base_host()
        search_url = f"https://{host}/jobs/search"

        records: list[dict | None] = []
        seen_urls: set[str] = set()
        page = -1
        complete = True
        stop_reason = STOP_EXHAUSTED

        while len(records) < self.max_jobs:
            page += 1
            try:
                html_text = http_client.get_text(
                    search_url,
                    params={"pr": page, "in_iframe": 1},
                    headers={"Accept": "text/html,application/xhtml+xml"},
                )
            except Exception as exc:
                if page == 0:
                    raise CollectorUnavailable(f"iCIMS search unavailable: {exc}") from exc
                self.log.warning("%s: iCIMS page %s failed (%s); marking incomplete",
                                 self.company, page, exc)
                complete, stop_reason = False, STOP_PAGE_FAILED
                break

            page_records: list[dict | None] = []

            # JSON-LD first: it carries real dates, which the HTML rows do not.
            for node in iter_jsonld_jobs(html_text):
                page_records.append(
                    self.record(
                        title=node.get("title"),
                        location=jsonld_location(node),
                        date_posted=node.get("datePosted"),
                        job_url=node.get("url") or node.get("@id"),
                        employment_type=node.get("employmentType"),
                        description=node.get("description"),
                    )
                )

            if not page_records:
                for link in extract_job_links(html_text, search_url, selector="a.iCIMS_Anchor"):
                    page_records.append(
                        self.record(
                            title=link["title"],
                            location=link.get("location"),
                            date_posted=link.get("date_posted"),
                            job_url=link["job_url"],
                        )
                    )

            if not page_records:
                for link in extract_job_links(html_text, search_url):
                    page_records.append(
                        self.record(
                            title=link["title"],
                            location=link.get("location"),
                            date_posted=link.get("date_posted"),
                            job_url=link["job_url"],
                        )
                    )

            fresh = [r for r in page_records if r and r["job_url"] not in seen_urls]
            if not fresh:
                stop_reason = STOP_NO_NEW_ROWS
                break
            for record in fresh:
                seen_urls.add(record["job_url"])
            records.extend(fresh)
        else:
            complete, stop_reason = False, STOP_BUDGET

        if not records:
            raise CollectorUnavailable("iCIMS search returned zero jobs")
        jobs = self.finalize(records)
        if not complete:
            self.log.warning("%s: iCIMS scrape INCOMPLETE (%s) - collected %s",
                             self.company, stop_reason, len(jobs))
        return CollectionResult(jobs=jobs, complete=complete,
                                pages_fetched=page + 1, stop_reason=stop_reason)
