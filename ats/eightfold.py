"""Eightfold AI collector - public apply API.

    GET https://{host}/api/apply/v2/jobs?domain={domain}&start=0&num=50

Eightfold powers many branded career sites (the page is a thin shell over this
endpoint). ``t_update`` is epoch seconds.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import EIGHTFOLD
from normalize import join_location

PAGE_SIZE = 50


class EightfoldCollector(ATSCollector):
    provider = EIGHTFOLD

    def _host(self) -> str:
        if self.host:
            return self.host
        if self.url:
            return urlsplit(self.url).netloc
        raise CollectorUnavailable("No Eightfold host available")

    def _domain(self) -> str:
        """The ``domain`` query parameter Eightfold partitions jobs by.

        ``self.tenant`` already carries this when the source URL had an
        explicit ``?domain=`` param (detector.py extracts it for Eightfold
        specifically, since the host is frequently just the generic
        "app.eightfold.ai" shell rather than a company-specific one - using
        the host in that case would query the wrong company entirely).
        Otherwise, derive it from the career host by stripping the leading
        label (careers.acme.com -> acme.com).
        """
        if self.tenant and "." in self.tenant:
            return self.tenant

        host = self._host()
        labels = host.split(".")
        if len(labels) > 2 and labels[0] in {"careers", "jobs", "www", "apply", "app"}:
            return ".".join(labels[1:])
        return host

    @staticmethod
    def _location(position: dict[str, Any]) -> str | None:
        if position.get("location"):
            return str(position["location"])
        locations = position.get("locations")
        if isinstance(locations, list) and locations:
            return join_location(*[str(loc) for loc in locations[:3]])
        return None

    def collect(self) -> list[dict]:
        host = self._host()
        endpoint = f"https://{host}/api/apply/v2/jobs"
        domain = self._domain()

        records: list[dict | None] = []
        start = 0
        total: int | None = None

        for page in range(self.max_pages):
            params = {
                "domain": domain,
                "start": start,
                "num": PAGE_SIZE,
                "sort_by": "timestamp",
            }
            try:
                data = http_client.get_json(endpoint, params=params)
            except Exception as exc:
                if page == 0:
                    raise CollectorUnavailable(f"Eightfold API unavailable: {exc}") from exc
                self.log.warning("%s: Eightfold page %s failed (%s)", self.company, page, exc)
                break

            if not isinstance(data, dict):
                raise CollectorUnavailable("Eightfold returned a non-object response")

            positions = data.get("positions") or []
            if total is None:
                total = data.get("count")
            if not positions:
                break

            for position in positions:
                if not isinstance(position, dict):
                    continue
                records.append(
                    self.record(
                        title=position.get("name") or position.get("title"),
                        location=self._location(position),
                        date_posted=position.get("t_create") or position.get("t_update"),
                        job_url=position.get("canonicalPositionUrl")
                        or position.get("positionUrl"),
                        employment_type=position.get("type"),
                        description=position.get("job_description"),
                    )
                )

            start += PAGE_SIZE
            if total is not None and start >= int(total):
                break

        if not records:
            raise CollectorUnavailable("Eightfold API returned zero positions")
        return self.finalize(records)
