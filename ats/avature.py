"""Avature collector - SearchJobs pages.

Avature career portals are server-rendered at::

    https://{tenant}.avature.net/careers/SearchJobs/?jobRecordsPerPage=100&jobOffset=0

Parsing is JSON-LD first (real dates when present), then the ``JobDetail``
anchor markup. Some tenants also honour ``?format=json``, but this collector
does not use it - an earlier version of this docstring claimed it was "tried
first", which was never true of the code.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.pagination import PageRequest, paginate
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

    def _page(self, search_url: str, request: PageRequest):
        html_text = http_client.get_text(
            search_url,
            params={"jobRecordsPerPage": request.page_size, "jobOffset": request.offset},
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
                            job_url=link["job_url"])
                for link in extract_job_links(html_text, search_url,
                                              selector='a[href*="JobDetail"]')
            ]
            rows = [r for r in rows if r]
        return rows, None

    def collect(self) -> CollectionResult:
        host = self._host()
        search_url = f"https://{host}/careers/SearchJobs/"

        try:
            walk = paginate(
                lambda request: self._page(search_url, request),
                page_size=PAGE_SIZE, max_jobs=self.max_jobs,
                key=lambda row: row["job_url"],
                label=f"{self.company}/avature",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"Avature search unavailable: {exc}") from exc

        if not walk.items:
            raise CollectorUnavailable("Avature search returned zero jobs")
        return self.result(walk, walk.items)
