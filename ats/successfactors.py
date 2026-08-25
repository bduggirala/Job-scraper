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
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.pagination import PageRequest, paginate
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

    def _page(self, search_url: str, request: PageRequest):
        html_text = http_client.get_text(
            search_url,
            params={"q": "", "sortColumn": "referencedate", "sortDirection": "desc",
                    "startrow": request.offset},
            headers={"Accept": "text/html"},
        )
        rows = [
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
        rows = [r for r in rows if r]
        if not rows:
            rows = [
                self.record(title=link["title"], location=link.get("location"),
                            date_posted=link.get("date_posted"), job_url=link["job_url"])
                for link in extract_job_links(html_text, search_url,
                                              selector='a[href*="/job/"]')
            ]
            rows = [r for r in rows if r]
        return rows, None

    def collect(self) -> CollectionResult:
        search_url = self._search_url()

        try:
            walk = paginate(
                lambda request: self._page(search_url, request),
                page_size=PAGE_STEP, max_jobs=self.max_jobs,
                key=lambda row: row["job_url"],
                label=f"{self.company}/successfactors",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"SuccessFactors search unavailable: {exc}") from exc

        if not walk.items:
            raise CollectorUnavailable("SuccessFactors search returned zero jobs")
        return self.result(walk, walk.items)
