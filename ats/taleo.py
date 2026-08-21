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
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import TALEO

ORC_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
ORC_PAGE_SIZE = 200


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

    def _collect_oracle_cloud(self) -> list[dict]:
        host = self.host or urlsplit(self.url or "").netloc
        if not host:
            raise CollectorUnavailable("No Oracle Cloud host available")

        endpoint = f"https://{host}{ORC_PATH}"
        site = self._site_number()
        records: list[dict | None] = []
        offset = 0
        total: int | None = None

        for page in range(self.max_pages):
            finder = (
                f"findReqs;siteNumber={site},limit={ORC_PAGE_SIZE},"
                f"offset={offset},sortBy=POSTING_DATES_DESC"
            )
            try:
                data = http_client.get_json(
                    endpoint,
                    # expand=requisitionList is required: without it the
                    # response carries only facet counts and TotalJobsCount,
                    # and the job array is omitted entirely.
                    params={
                        "onlyData": "true",
                        "expand": "requisitionList",
                        "finder": finder,
                    },
                    headers={"Accept": "application/json"},
                )
            except Exception as exc:
                if page == 0:
                    raise CollectorUnavailable(f"Oracle Cloud API unavailable: {exc}") from exc
                break

            items = (data or {}).get("items") or []
            if not items:
                break

            if total is None:
                total = items[0].get("TotalJobsCount")

            requisitions = items[0].get("requisitionList") or []
            if not requisitions:
                break

            for req in requisitions:
                if not isinstance(req, dict):
                    continue
                job_id = req.get("Id") or req.get("RequisitionId")
                job_url = (
                    f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
                    f"/job/{job_id}" if job_id else None
                )
                workplace = req.get("WorkplaceType")
                records.append(
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

            offset += ORC_PAGE_SIZE
            if len(requisitions) < ORC_PAGE_SIZE:
                break
            if total is not None and offset >= int(total):
                break

        if not records:
            raise CollectorUnavailable("Oracle Cloud API returned zero requisitions")

        self.log.debug("%s: Oracle Cloud reported total=%s, collected=%s",
                       self.company, total, len(records))
        return self.finalize(records)

    # -- Legacy Taleo career section --------------------------------------
    @staticmethod
    def _pick_from_columns(columns: Any, keys: list[str]) -> dict[str, Any]:
        """Map a positional ``column`` array onto named keys when possible."""
        values = columns if isinstance(columns, list) else []
        mapped: dict[str, Any] = {}
        for index, key in enumerate(keys):
            mapped[key] = values[index] if index < len(values) else None
        return mapped

    def _collect_legacy_taleo(self) -> list[dict]:
        host = self.host
        if not host:
            raise CollectorUnavailable("No Taleo host available")

        endpoint = f"https://{host}/careersection/rest/jobboard/searchjobs"
        records: list[dict | None] = []

        for page in range(1, min(self.max_pages, 10) + 1):
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
                "pageNo": page,
            }
            try:
                data = http_client.post_json(
                    endpoint,
                    payload,
                    params={"lang": "en", "portal": self.site or ""},
                    headers={"Accept": "application/json"},
                )
            except Exception as exc:
                if page == 1:
                    raise CollectorUnavailable(f"Taleo searchjobs unavailable: {exc}") from exc
                break

            requisitions = (data or {}).get("requisitionList") or []
            if not requisitions:
                break

            for req in requisitions:
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
                records.append(
                    self.record(
                        title=mapped.get("title"),
                        location=mapped.get("location"),
                        date_posted=mapped.get("date_posted"),
                        job_url=job_url,
                        description=req.get("descriptionTeaser"),
                    )
                )

            if len(requisitions) < 25:
                break

        if not records:
            raise CollectorUnavailable("Taleo searchjobs returned zero requisitions")
        return self.finalize(records)

    def collect(self) -> list[dict]:
        if self._is_oracle_cloud():
            return self._collect_oracle_cloud()
        try:
            return self._collect_legacy_taleo()
        except CollectorUnavailable:
            # Some tenants sit on oraclecloud behind a taleo.net vanity host.
            return self._collect_oracle_cloud()
