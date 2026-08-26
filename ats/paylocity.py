"""Paylocity Recruiting collector.

Paylocity job boards live at::

    https://recruiting.paylocity.com/recruiting/jobs/All/{companyGuid}/{slug}

There is an internal JSON endpoint backing that page which this collector
tries first; when it is absent or reshaped, the collector falls back to
parsing the server-rendered list (and any JSON-LD), and finally raises
CollectorUnavailable so Playwright takes over.
"""

from __future__ import annotations

import json
import re

import http_client
from ats.base import (
    STOP_EXHAUSTED,
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
from ats.pagination import PageRequest, paginate
from ats.detector import PAYLOCITY
from ats.html_utils import extract_job_links, iter_jsonld_jobs, jsonld_location

BASE = "https://recruiting.paylocity.com"
PAGE_SIZE = 200

#: Paylocity renders its board client-side, but ships the whole list inside the
#: HTML as ``window.pageData = {... "Jobs": [...] ...}``. Nothing else on the
#: page names a posting: there is no JSON-LD, and the job links are built by
#: JavaScript, so an anchor scrape finds zero. Reading this blob is the only
#: way to see the board without a browser - and the browser found zero too,
#: because the rows render into a virtualized list.
_PAGE_DATA_RE = re.compile(r"window\.pageData\s*=\s*", re.I)

#: ``window.ATSJobDetailsBaseUrl`` on the same page. Hard-coded as the fallback
#: because the posting URL has to be built from ``JobId`` - the row itself
#: carries no link.
_DETAILS_PATH = "/Recruiting/Jobs/Details/"


class PaylocityCollector(ATSCollector):
    provider = PAYLOCITY

    def _guid(self) -> str:
        guid = self.identifier or self.tenant
        if not guid:
            raise CollectorUnavailable("No Paylocity company GUID in URL")
        return str(guid)

    def _api_page(self, endpoint: str, guid: str, request: PageRequest):
        data = http_client.get_json(
            endpoint,
            params={"companyId": guid, "pageSize": request.page_size,
                    "pageNumber": request.page_number},
            headers={"Accept": "application/json"},
        )
        jobs = (
            data if isinstance(data, list)
            else (data or {}).get("jobs") or (data or {}).get("items")
        )
        if not isinstance(jobs, list):
            raise CollectorUnavailable("Paylocity API returned no usable job list")
        rows = [
            self.record(
                title=job.get("title") or job.get("jobTitle"),
                location=job.get("location") or job.get("locationName"),
                date_posted=job.get("publishedDate") or job.get("postedDate"),
                job_url=job.get("url")
                or (f"{BASE}/recruiting/jobs/Details/{job.get('jobId') or job.get('id')}"
                    if (job.get("jobId") or job.get("id")) else None),
                employment_type=job.get("employmentType") or job.get("jobType"),
                description=job.get("description"),
            )
            for job in jobs
            if isinstance(job, dict)
        ]
        return [r for r in rows if r], None

    def _try_api(self, guid: str) -> CollectionResult:
        endpoint = f"{BASE}/recruiting/v2/api/jobs"
        walk = paginate(
            lambda request: self._api_page(endpoint, guid, request),
            page_size=PAGE_SIZE, max_jobs=self.max_jobs,
            key=lambda row: row["job_url"],
            label=f"{self.company}/paylocity",
        )
        if not walk.items:
            raise CollectorUnavailable("Paylocity API rows lacked title/url")
        return self.result(walk, walk.items)

    def _from_page_data(self, html_text: str) -> list[dict]:
        """Rows from the ``window.pageData`` blob, or [] when it is absent.

        Uses ``framework_data._balanced_json`` rather than a regex: the blob
        contains job descriptions, which contain braces, so a non-greedy match
        stops in the middle of the first posting.
        """
        from ats.framework_data import _balanced_json

        match = _PAGE_DATA_RE.search(html_text)
        if not match:
            return []
        brace = html_text.find("{", match.end())
        if brace == -1:
            return []
        blob = _balanced_json(html_text, brace)
        if not blob:
            return []
        try:
            payload = json.loads(blob)
        except (ValueError, TypeError):
            return []

        jobs = payload.get("Jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []

        records = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = job.get("JobId") or job.get("jobId")
            if not job_id:
                continue
            # The row carries no link of its own; the board builds one from
            # JobId at render time, and this is that same construction.
            records.append(
                self.record(
                    title=job.get("JobTitle") or job.get("Title"),
                    location=self._page_data_location(job),
                    date_posted=job.get("PublishedDate") or job.get("publishedDate"),
                    job_url=f"{BASE}{_DETAILS_PATH}{job_id}",
                    employment_type=job.get("EmploymentType") or job.get("JobType"),
                    description=job.get("Description"),
                    remote=job.get("IsRemote") if isinstance(job.get("IsRemote"), bool) else None,
                )
            )
        return [r for r in records if r]

    @staticmethod
    def _page_data_location(job: dict) -> str | None:
        """Prefer the structured address over the branch nickname.

        ``LocationName`` is an internal label ("Richardson-Campbell Rd",
        "Wylie-Hwy 78") that names no state, so the DFW matcher cannot judge
        it; the sibling ``Location`` object carries City/State properly.
        """
        address = job.get("JobLocation") or job.get("Location")
        if isinstance(address, dict):
            city = address.get("City")
            state = address.get("State")
            if city and state:
                return f"{city}, {state}"
            if city:
                return str(city)
        name = job.get("LocationName") or job.get("locationName")
        return str(name) if name else None

    def _try_html(self, guid: str) -> CollectionResult:
        list_url = self.url or f"{BASE}/recruiting/jobs/All/{guid}"
        html_text = http_client.get_text(list_url, headers={"Accept": "text/html"})

        # The embedded blob first: it is the only complete view of the board,
        # and it carries dates and structured locations the other two paths
        # cannot see.
        records = self._from_page_data(html_text)
        if records:
            self.log.debug("%s: Paylocity pageData carried %s job(s)",
                           self.company, len(records))
            return CollectionResult(jobs=self.finalize(records),
                                    pages_fetched=1, stop_reason=STOP_EXHAUSTED)

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
