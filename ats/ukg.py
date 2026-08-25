"""UKG Pro / UltiPro collector - JobBoard search endpoint.

    POST https://recruiting.ultipro.com/{tenant}/JobBoard/{boardGuid}/JobBoardView/LoadSearchResults
    {"opportunitySearch": {"Top": 100, "Skip": 0, "QueryString": "", ...}}

This endpoint backs the public job board UI. It is stable in practice but is
not a documented product API, so field names are read defensively and any
shape mismatch raises CollectorUnavailable to trigger the Playwright fallback.
"""

from __future__ import annotations

from typing import Any

import http_client
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.detector import UKG
from ats.pagination import PageRequest, paginate
from normalize import join_location

PAGE_SIZE = 100
BASE = "https://recruiting.ultipro.com"


class UKGCollector(ATSCollector):
    provider = UKG

    def _base(self) -> str:
        """Origin serving this tenant's job board.

        UKG Pro Recruiting is reachable both at the shared
        ``recruiting.ultipro.com`` host and at per-tenant hosts such as
        ``recruiting2.ultipro.com`` or ``gamestop.rec.pro.ukg.net`` -
        identical URL shape and API, so the host from the detected URL is
        always used when present rather than assuming the shared host.
        """
        if self.host:
            return f"https://{self.host}"
        return BASE

    def _coordinates(self) -> tuple[str, str]:
        tenant, board = self.tenant, self.site
        if not tenant or not board:
            raise CollectorUnavailable(
                f"Incomplete UKG coordinates (tenant={tenant}, board={board})"
            )
        return str(tenant), str(board)

    @staticmethod
    def _location(opportunity: dict[str, Any]) -> str | None:
        locations = opportunity.get("Locations")
        if not isinstance(locations, list) or not locations:
            return None

        parts: list[str] = []
        for entry in locations[:3]:
            if not isinstance(entry, dict):
                continue
            described = entry.get("LocalizedDescription") or entry.get("Description")
            if described:
                parts.append(str(described))
                continue
            address = entry.get("Address")
            if isinstance(address, dict):
                state = address.get("State")
                state_code = state.get("Code") if isinstance(state, dict) else state
                country = address.get("Country")
                country_code = country.get("Code") if isinstance(country, dict) else country
                joined = join_location(address.get("City"), state_code, country_code)
                if joined:
                    parts.append(joined)
        return join_location(*parts) if parts else None

    def _job_url(self, tenant: str, board: str, opportunity: dict[str, Any]) -> str | None:
        for key in ("UrlJobPosting", "JobUrl", "Url"):
            value = opportunity.get(key)
            if value:
                return str(value)
        opportunity_id = opportunity.get("Id") or opportunity.get("OpportunityId")
        if opportunity_id:
            return f"{self._base()}/{tenant}/JobBoard/{board}/OpportunityDetail?opportunityId={opportunity_id}"
        return None

    def _fetch_page(self, endpoint: str, base: str, request: PageRequest):
        payload = {
            "opportunitySearch": {
                "Top": request.page_size,
                "Skip": request.offset,
                "QueryString": "",
                "OrderBy": [
                    {"Value": "postedDateDesc", "PropertyName": "PostedDate",
                     "Ascending": False}
                ],
                "Filters": [],
            },
            "matchCriteria": {
                "PreferredJobs": [], "Educations": [],
                "LicenseAndCertifications": [], "Skills": [],
            },
        }
        data = http_client.post_json(
            endpoint, payload, headers={"Accept": "application/json", "Origin": base},
        )
        if not isinstance(data, dict):
            raise CollectorUnavailable("UKG returned a non-object response")
        opportunities = data.get("opportunities")
        if not isinstance(opportunities, list):
            raise CollectorUnavailable("UKG response missing 'opportunities'")
        return opportunities, data.get("totalCount") or data.get("TotalCount")

    def collect(self) -> CollectionResult:
        tenant, board = self._coordinates()
        base = self._base()
        endpoint = f"{base}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults"

        try:
            walk = paginate(
                lambda request: self._fetch_page(endpoint, base, request),
                page_size=PAGE_SIZE, max_jobs=self.max_jobs,
                label=f"{self.company}/ukg",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"UKG job board unavailable: {exc}") from exc

        records = [
            self.record(
                title=opportunity.get("Title") or opportunity.get("JobTitle"),
                location=self._location(opportunity),
                date_posted=opportunity.get("PostedDate") or opportunity.get("CreatedDate"),
                job_url=self._job_url(tenant, board, opportunity),
                employment_type=opportunity.get("EmploymentType") or opportunity.get("FullTime"),
                description=opportunity.get("Description"),
            )
            for opportunity in walk.items
            if isinstance(opportunity, dict)
        ]
        if not records:
            raise CollectorUnavailable("UKG job board returned zero opportunities")
        return self.result(walk, records)
