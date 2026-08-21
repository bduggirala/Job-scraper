"""Lever collector - public postings API.

    GET https://api.lever.co/v0/postings/{company}?mode=json

Returns a flat JSON array of postings. ``createdAt`` is epoch milliseconds.
"""

from __future__ import annotations

from typing import Any

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import LEVER

API_TEMPLATE = "https://api.lever.co/v0/postings/{company}"


class LeverCollector(ATSCollector):
    provider = LEVER

    def _token(self) -> str:
        token = self.identifier or self.tenant
        if not token:
            raise CollectorUnavailable("No Lever company token in URL")
        return str(token).strip("/")

    def collect(self) -> list[dict]:
        url = API_TEMPLATE.format(company=self._token())
        try:
            data = http_client.get_json(url, params={"mode": "json"})
        except Exception as exc:
            raise CollectorUnavailable(f"Lever API unavailable: {exc}") from exc

        if not isinstance(data, list) or not data:
            raise CollectorUnavailable("Lever API returned zero postings")

        records = []
        for posting in data:
            if not isinstance(posting, dict):
                continue
            categories: dict[str, Any] = posting.get("categories") or {}
            workplace = posting.get("workplaceType")

            records.append(
                self.record(
                    title=posting.get("text"),
                    location=categories.get("location"),
                    date_posted=posting.get("createdAt"),
                    job_url=posting.get("hostedUrl"),
                    apply_url=posting.get("applyUrl"),
                    employment_type=categories.get("commitment"),
                    remote=(workplace.lower() == "remote") if isinstance(workplace, str) else None,
                    description=posting.get("descriptionPlain") or posting.get("description"),
                )
            )
        return self.finalize(records)
