"""Ashby collector - public job board API.

    GET https://api.ashbyhq.com/posting-api/job-board/{board}

Returns ``{"jobs": [...]}`` in one response. Ashby is unusually complete: it
gives ``publishedAt``, ``isRemote`` and ``employmentType`` directly.
"""

from __future__ import annotations

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import ASHBY

API_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{board}"


class AshbyCollector(ATSCollector):
    provider = ASHBY

    def _board(self) -> str:
        board = self.identifier or self.tenant
        if not board:
            raise CollectorUnavailable("No Ashby board name in URL")
        return str(board).strip("/")

    def collect(self) -> list[dict]:
        url = API_TEMPLATE.format(board=self._board())
        try:
            data = http_client.get_json(url, params={"includeCompensation": "true"})
        except Exception as exc:
            raise CollectorUnavailable(f"Ashby API unavailable: {exc}") from exc

        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not jobs:
            raise CollectorUnavailable("Ashby API returned zero jobs")

        records = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            is_remote = job.get("isRemote")
            records.append(
                self.record(
                    title=job.get("title"),
                    location=job.get("location"),
                    date_posted=job.get("publishedAt") or job.get("updatedAt"),
                    job_url=job.get("jobUrl"),
                    apply_url=job.get("applyUrl"),
                    employment_type=job.get("employmentType"),
                    remote=bool(is_remote) if isinstance(is_remote, bool) else None,
                    description=job.get("descriptionPlain") or job.get("descriptionHtml"),
                )
            )
        return self.finalize(records)
