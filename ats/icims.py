"""iCIMS collector - server-rendered search pages.

iCIMS has no public JSON API. The public job list is server-rendered at::

    https://careers-{tenant}.icims.com/jobs/search?pr={page}&in_iframe=1

so this collector parses HTML (and any JSON-LD the page emits, which is more
reliable when present). Pagination walks ``pr`` until a page yields nothing new.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.pagination import PageRequest, paginate
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

    def _page(self, search_url: str, request: PageRequest):
        html_text = http_client.get_text(
            search_url,
            params={"pr": request.page_index, "in_iframe": 1},
            headers={"Accept": "text/html,application/xhtml+xml"},
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
            for selector in ("a.iCIMS_Anchor", None):
                links = extract_job_links(html_text, search_url, selector=selector)
                rows = [
                    self.record(title=link["title"], location=link.get("location"),
                                date_posted=link.get("date_posted"), job_url=link["job_url"])
                    for link in links
                ]
                rows = [r for r in rows if r]
                if rows:
                    break
        return rows, None

    def collect(self) -> CollectionResult:
        host = self._base_host()
        search_url = f"https://{host}/jobs/search"

        try:
            walk = paginate(
                lambda request: self._page(search_url, request),
                page_size=ROWS_PER_PAGE, max_jobs=self.max_jobs,
                key=lambda row: row["job_url"],
                label=f"{self.company}/icims",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"iCIMS search unavailable: {exc}") from exc

        if not walk.items:
            raise CollectorUnavailable("iCIMS search returned zero jobs")
        return self.result(walk, walk.items)
