"""Taleo / Oracle collector.

Two distinct Oracle recruiting products share this module:

1. **Oracle Cloud Recruiting (ORC)** - modern, and the one with a genuinely
   clean REST API::

       GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
           ?finder=findReqs;siteNumber=CX_1,limit=200,sortBy=POSTING_DATES_DESC

2. **Legacy Taleo career sections** - ``{tenant}.taleo.net/careersection/...``.
   The ``searchjobs`` REST endpoint returns rows as a positional ``column``
   array whose ordering is configured per-portal, so parsing is heuristic.

Both paths raise CollectorUnavailable on any shape mismatch so the router
falls back to Playwright instead of emitting garbage rows.

Both also walk their pages through :func:`ats.pagination.paginate` rather than
by hand. Ten of the workbook's companies route here, and the hand-rolled loops
were the last ones in the codebase without per-page retry (one 503 mid-walk
suppressed that company's removal sync), total reconciliation (ORC reports
``TotalJobsCount`` and nothing compared the harvest against it) or
repeated-page detection.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import (
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
from ats.detector import TALEO
from ats.pagination import PageRequest, paginate

ORC_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
ORC_PAGE_SIZE = 200
#: Page size sent to the legacy searchjobs endpoint. Sent explicitly so
#: end-of-results can be judged against a size we chose rather than a guess.
LEGACY_PAGE_SIZE = 25


class TaleoCollector(ATSCollector):
    provider = TALEO

    # -- Oracle Cloud Recruiting -----------------------------------------
    def _is_oracle_cloud(self) -> bool:
        """True for Oracle Cloud Recruiting, including branded vanity hosts.

        Branded ORC sites (careers.honeywell.com/en/sites/Honeywell) carry
        neither an oraclecloud.com host nor an /hcmUI/ path, so the
        ``/sites/{site}`` shape is treated as an ORC marker too.
        """
        host = (self.host or "").lower()
        url = (self.url or "").lower()
        if "oraclecloud.com" in host or "/hcmui/candidateexperience" in url:
            return True
        return bool(re.search(r"/sites/[A-Za-z0-9_]+", url)) and ".taleo.net" not in host

    def _site_number(self) -> str:
        """Extract the CX site number from the career-site URL, else default."""
        match = re.search(r"/sites/([A-Za-z0-9_]+)", self.url or "")
        if match:
            return match.group(1)
        return "CX_1"

    def _orc_page(self, endpoint: str, host: str, site: str, request: PageRequest):
        """One ORC page: ``(rows, TotalJobsCount)``."""
        finder = (
            f"findReqs;siteNumber={site},limit={request.page_size},"
            f"offset={request.offset},sortBy=POSTING_DATES_DESC"
        )
        data = http_client.get_json(
            endpoint,
            # expand=requisitionList is required: without it the response
            # carries only facet counts and TotalJobsCount, and the job array
            # is omitted entirely.
            params={
                "onlyData": "true",
                "expand": "requisitionList",
                "finder": finder,
            },
            headers={"Accept": "application/json"},
        )

        items = (data or {}).get("items") or []
        if not items:
            return [], None

        total = items[0].get("TotalJobsCount")
        rows = []
        for req in items[0].get("requisitionList") or []:
            if not isinstance(req, dict):
                continue
            job_id = req.get("Id") or req.get("RequisitionId")
            job_url = (
                f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
                f"/job/{job_id}" if job_id else None
            )
            workplace = req.get("WorkplaceType")
            rows.append(
                self.record(
                    title=req.get("Title"),
                    location=req.get("PrimaryLocation") or req.get("Location"),
                    date_posted=req.get("PostedDate") or req.get("PostingStartDate"),
                    job_url=job_url,
                    employment_type=req.get("JobType") or req.get("WorkerType")
                    or req.get("JobSchedule"),
                    remote=("remote" in workplace.lower())
                    if isinstance(workplace, str) and workplace else None,
                    description=req.get("ShortDescriptionStr"),
                )
            )
        return [r for r in rows if r], total

    def _collect_oracle_cloud(self) -> CollectionResult:
        host = self.host or urlsplit(self.url or "").netloc
        if not host:
            raise CollectorUnavailable("No Oracle Cloud host available")

        endpoint = f"https://{host}{ORC_PATH}"
        site = self._site_number()

        try:
            walk = paginate(
                lambda request: self._orc_page(endpoint, host, site, request),
                page_size=ORC_PAGE_SIZE, max_jobs=self.max_jobs,
                key=lambda row: row["job_url"],
                label=f"{self.company}/oracle-cloud",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"Oracle Cloud API unavailable: {exc}") from exc

        if not walk.items:
            raise CollectorUnavailable("Oracle Cloud API returned zero requisitions")
        return self.result(walk, walk.items)

    # -- Legacy Taleo career section --------------------------------------
    @staticmethod
    def _pick_from_columns(columns: Any, keys: list[str]) -> dict[str, Any]:
        """Map a positional ``column`` array onto named keys when possible."""
        values = columns if isinstance(columns, list) else []
        mapped: dict[str, Any] = {}
        for index, key in enumerate(keys):
            mapped[key] = values[index] if index < len(values) else None
        return mapped

    def _legacy_page(self, endpoint: str, host: str, request: PageRequest):
        """One legacy career-section page: ``(rows, None)``.

        The endpoint reports no total, so the walk ends on a short page - and
        the shared controller judges "short" against what page one actually
        served rather than what was asked for, which is what keeps a portal
        capping below ``LEGACY_PAGE_SIZE`` from reading as a one-page employer.
        """
        payload = {
            "multilineEnabled": False,
            "sortingSelection": {
                "sortBySelectionParam": "3",
                "ascendingSortingOrder": "false",
            },
            "fieldData": {
                "fields": {"KEYWORD": "", "LOCATION": ""},
                "valid": True,
            },
            "filterSelectionParam": {"searchFilterSelections": []},
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
            "pageNo": request.page_number,
            # Sent explicitly so end-of-results is judged against a size we
            # chose. Previously nothing was sent and the loop compared against
            # a hard-coded 25, so a portal serving 15 a page stopped after page
            # one and reported success.
            "pageSize": request.page_size,
        }
        data = http_client.post_json(
            endpoint,
            payload,
            params={"lang": "en", "portal": self.site or ""},
            headers={"Accept": "application/json"},
        )

        rows = []
        for req in (data or {}).get("requisitionList") or []:
            if not isinstance(req, dict):
                continue
            # Column order is portal-configured; this is the common default.
            mapped = self._pick_from_columns(
                req.get("column"), ["title", "location", "date_posted"]
            )
            job_id = req.get("jobId") or req.get("contestNo")
            job_url = (
                f"https://{host}/careersection/{self.site or '2'}/jobdetail.ftl?job={job_id}"
                if job_id else None
            )
            rows.append(
                self.record(
                    title=mapped.get("title"),
                    location=mapped.get("location"),
                    date_posted=mapped.get("date_posted"),
                    job_url=job_url,
                    description=req.get("descriptionTeaser"),
                )
            )
        return [r for r in rows if r], None

    def _collect_legacy_taleo(self) -> CollectionResult:
        host = self.host
        if not host:
            raise CollectorUnavailable("No Taleo host available")

        endpoint = f"https://{host}/careersection/rest/jobboard/searchjobs"

        try:
            walk = paginate(
                lambda request: self._legacy_page(endpoint, host, request),
                page_size=LEGACY_PAGE_SIZE, max_jobs=self.max_jobs,
                key=lambda row: row["job_url"],
                label=f"{self.company}/taleo",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"Taleo searchjobs unavailable: {exc}") from exc

        if not walk.items:
            raise CollectorUnavailable("Taleo searchjobs returned zero requisitions")
        return self.result(walk, walk.items)

    def collect(self) -> CollectionResult:
        if self._is_oracle_cloud():
            return self._collect_oracle_cloud()
        try:
            return self._collect_legacy_taleo()
        except CollectorUnavailable as legacy_exc:
            # Some tenants sit on oraclecloud behind a taleo.net vanity host.
            try:
                return self._collect_oracle_cloud()
            except CollectorUnavailable as orc_exc:
                # Report both: the legacy failure is usually the real reason
                # (e.g. careerSectionUnAvailable), and surfacing only the ORC
                # 404 sends debugging down the wrong path.
                raise CollectorUnavailable(
                    f"legacy Taleo: {legacy_exc}; Oracle Cloud fallback: {orc_exc}"
                ) from legacy_exc
