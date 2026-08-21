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
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import SUCCESSFACTORS
from ats.html_utils import extract_job_links, iter_jsonld_jobs, jsonld_location

PAGE_STEP = 25
MAX_PAGES = 8


class SuccessFactorsCollector(ATSCollector):
    provider = SUCCESSFACTORS

    def _search_url(self) -> str:
        if not self.url:
            raise CollectorUnavailable("No SuccessFactors URL available")
        parts = urlsplit(self.url)
        if not parts.netloc:
            raise CollectorUnavailable("Unparseable SuccessFactors URL")
        return urlunsplit((parts.scheme or "https", parts.netloc, "/search/", "", ""))

    def collect(self) -> list[dict]:
        search_url = self._search_url()
        records: list[dict | None] = []
        seen: set[str] = set()

        for page in range(min(self.max_pages, MAX_PAGES)):
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
                break
            for record in fresh:
                seen.add(record["job_url"])
            records.extend(fresh)

        if not records:
            raise CollectorUnavailable("SuccessFactors search returned zero jobs")
        return self.finalize(records)
