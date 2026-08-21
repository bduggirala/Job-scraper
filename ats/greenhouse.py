"""Greenhouse collector - public job board API.

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

Returns every posting in one response (no pagination). ``content=true`` also
returns the HTML description and the ``updated_at`` timestamp.
"""

from __future__ import annotations

from typing import Any

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import GREENHOUSE

API_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class GreenhouseCollector(ATSCollector):
    provider = GREENHOUSE

    def _token(self) -> str:
        token = self.identifier or self.tenant
        if not token:
            raise CollectorUnavailable("No Greenhouse board token in URL")
        return str(token).strip("/")

    @staticmethod
    def _location(job: dict[str, Any]) -> str | None:
        location = job.get("location")
        if isinstance(location, dict):
            return location.get("name")
        if isinstance(location, str):
            return location

        offices = job.get("offices") or []
        names = [o.get("name") for o in offices if isinstance(o, dict) and o.get("name")]
        return ", ".join(names) if names else None

    @staticmethod
    def _employment_type(job: dict[str, Any]) -> str | None:
        for item in job.get("metadata") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name", "")).lower() in {"employment type", "job type"}:
                value = item.get("value")
                if isinstance(value, list):
                    return ", ".join(str(v) for v in value if v) or None
                return str(value) if value else None
        return None

    def collect(self) -> list[dict]:
        url = API_TEMPLATE.format(token=self._token())
        try:
            data = http_client.get_json(url, params={"content": "true"})
        except Exception as exc:
            raise CollectorUnavailable(f"Greenhouse API unavailable: {exc}") from exc

        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not jobs:
            raise CollectorUnavailable("Greenhouse API returned zero jobs")

        records = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            records.append(
                self.record(
                    title=job.get("title"),
                    location=self._location(job),
                    # first_published is the true posting date; updated_at is a fallback.
                    date_posted=job.get("first_published") or job.get("updated_at"),
                    job_url=job.get("absolute_url"),
                    employment_type=self._employment_type(job),
                    description=job.get("content"),
                )
            )
        return self.finalize(records)
