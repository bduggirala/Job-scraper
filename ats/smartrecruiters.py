"""SmartRecruiters collector - public postings API.

    GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=0

Paginated via ``offset``; ``totalFound`` bounds the walk. The listing does not
include a description, so ``description`` stays None rather than being faked.
"""

from __future__ import annotations

from typing import Any

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import SMARTRECRUITERS
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

    def collect(self) -> list[dict]:
        company_id = self._company_id()
        url = API_TEMPLATE.format(company=company_id)

        records: list[dict | None] = []
        offset = 0
        total: int | None = None

        for page in range(self.max_pages):
            try:
                data = http_client.get_json(
                    url, params={"limit": PAGE_SIZE, "offset": offset}
                )
            except Exception as exc:
                if page == 0:
                    raise CollectorUnavailable(f"SmartRecruiters API unavailable: {exc}") from exc
                self.log.warning("%s: SmartRecruiters page %s failed (%s)", self.company, page, exc)
                break

            if not isinstance(data, dict):
                raise CollectorUnavailable("SmartRecruiters returned a non-object response")

            postings = data.get("content") or []
            if total is None:
                total = data.get("totalFound")
            if not postings:
                break

            for posting in postings:
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

            offset += PAGE_SIZE
            if total is not None and offset >= int(total):
                break

        if not records:
            raise CollectorUnavailable("SmartRecruiters API returned zero postings")
        return self.finalize(records)
