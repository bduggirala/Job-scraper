"""SmartRecruiters collector - public postings API.

    GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=0

Paginated via ``offset``; ``totalFound`` bounds the walk. The listing does not
include a description, so ``description`` stays None rather than being faked.
"""

from __future__ import annotations

from typing import Any

import http_client
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.detector import SMARTRECRUITERS
from ats.pagination import PageRequest, paginate
from normalize import join_location

API_TEMPLATE = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
PUBLIC_JOB_URL = "https://jobs.smartrecruiters.com/{company}/{job_id}"
PAGE_SIZE = 100


class SmartRecruitersCollector(ATSCollector):
    provider = SMARTRECRUITERS

    def _company_id(self) -> str:
        company_id = self.identifier or self.tenant
        if not company_id:
            raise CollectorUnavailable("No SmartRecruiters company id in URL")
        return str(company_id).strip("/")

    @staticmethod
    def _location(posting: dict[str, Any]) -> tuple[str | None, bool | None]:
        location = posting.get("location")
        if not isinstance(location, dict):
            return None, None
        text = join_location(
            location.get("city"), location.get("region"), location.get("country")
        )
        remote = location.get("remote")
        return text, bool(remote) if isinstance(remote, bool) else None

    def _fetch_page(self, url: str, request: PageRequest):
        data = http_client.get_json(
            url, params={"limit": request.page_size, "offset": request.offset}
        )
        if not isinstance(data, dict):
            raise CollectorUnavailable("SmartRecruiters returned a non-object response")
        return data.get("content") or [], data.get("totalFound")

    def collect(self) -> CollectionResult:
        company_id = self._company_id()
        url = API_TEMPLATE.format(company=company_id)

        try:
            walk = paginate(
                lambda request: self._fetch_page(url, request),
                page_size=PAGE_SIZE, max_jobs=self.max_jobs,
                label=f"{self.company}/smartrecruiters",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"SmartRecruiters API unavailable: {exc}") from exc

        records = []
        for posting in walk.items:
            if not isinstance(posting, dict):
                continue
            location_text, remote = self._location(posting)
            job_id = posting.get("id")
            records.append(
                self.record(
                    title=posting.get("name"),
                    location=location_text,
                    date_posted=posting.get("releasedDate") or posting.get("createdOn"),
                    job_url=PUBLIC_JOB_URL.format(company=company_id, job_id=job_id)
                    if job_id else posting.get("ref"),
                    employment_type=(posting.get("typeOfEmployment") or {}).get("label")
                    if isinstance(posting.get("typeOfEmployment"), dict) else None,
                    remote=remote,
                )
            )
        if not records:
            raise CollectorUnavailable("SmartRecruiters API returned zero postings")
        return self.result(walk, records)
