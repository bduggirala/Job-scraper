"""Jibe (iCIMS-owned) collector - public JSON job-search API.

Jibe powers branded ``{tenant}.jibeapply.com`` careersites for iCIMS customers.
The page is a thin shell over a public JSON search endpoint::

    GET https://{host}/api/jobs?page={n}&limit={rpp}

which returns ``{"jobs": [{"data": {...}}], "totalCount": N, "count": N, ...}``.
Each ``data`` object carries ``slug``/``req_id``, ``title``, ``full_location``
(plus discrete ``city``/``state``/``country``), ``posted_date`` (ISO-8601),
``employment_type``, ``description``, and an iCIMS ``apply_url``. The job's own
page on the Jibe site is ``https://{host}/jobs/{slug}`` (site canonical), which
we use as the stable ``job_url``.

The endpoint caps ``limit`` at 100 (larger values return zero jobs), so we walk
``page`` at 100/row until a page yields no new slugs or we reach ``totalCount``.
Pagination is bounded by MAX_PAGES so a tenant that ignores it cannot spin
forever.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import JIBE
from normalize import join_location

# The API rejects limit > 100 by returning an empty ``jobs`` list, so 100 is the
# largest safe page size. 1,200-job tenants come back in ~12 requests.
RECORDS_PER_PAGE = 100
MAX_PAGES = 60


class JibeCollector(ATSCollector):
    provider = JIBE

    def _host(self) -> str:
        if self.host:
            return self.host
        if self.url:
            return urlsplit(self.url if "//" in self.url else f"https://{self.url}").netloc
        raise CollectorUnavailable("No Jibe host available")

    def _fetch_page(self, endpoint: str, page: int) -> dict[str, Any]:
        data = http_client.get_json(
            endpoint,
            params={"page": page, "limit": RECORDS_PER_PAGE},
            headers={"Accept": "application/json"},
        )
        if not isinstance(data, dict):
            raise CollectorUnavailable("Jibe API returned a non-object response")
        return data

    @staticmethod
    def _location(data: dict[str, Any]) -> str | None:
        if data.get("full_location"):
            return str(data["full_location"])
        return join_location(data.get("city"), data.get("state"), data.get("country"))

    def _row(self, host: str, data: dict[str, Any]) -> dict | None:
        slug = data.get("slug") or data.get("req_id")
        if not slug:
            return None
        return self.record(
            title=data.get("title"),
            location=self._location(data),
            date_posted=data.get("posted_date")
            or data.get("update_date")
            or data.get("create_date"),
            job_url=f"https://{host}/jobs/{slug}",
            apply_url=data.get("apply_url"),
            employment_type=data.get("employment_type"),
            description=data.get("description"),
        )

    def collect(self) -> list[dict]:
        host = self._host()
        endpoint = f"https://{host}/api/jobs"

        records: list[dict | None] = []
        seen: set[str] = set()
        total: int | None = None

        for page in range(1, MAX_PAGES + 1):
            try:
                data = self._fetch_page(endpoint, page)
            except CollectorUnavailable:
                raise
            except Exception as exc:
                if page == 1:
                    raise CollectorUnavailable(f"Jibe API unavailable: {exc}") from exc
                self.log.warning("%s: Jibe page %s failed (%s)", self.company, page, exc)
                break

            jobs = data.get("jobs") or []
            if total is None:
                total = data.get("totalCount") or data.get("count")

            fresh = []
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                row = self._row(host, job.get("data") or {})
                if row and row["job_url"] not in seen:
                    fresh.append(row)

            if not fresh:
                break
            for row in fresh:
                seen.add(row["job_url"])
            records.extend(fresh)

            if total is not None and len(seen) >= int(total):
                break

        if not records:
            raise CollectorUnavailable("Jibe API returned zero jobs")
        return self.finalize(records)
