"""Eightfold AI collector - public apply API.

    GET https://{host}/api/apply/v2/jobs?domain={domain}&start=0&num=50

Eightfold powers many branded career sites (the page is a thin shell over this
endpoint). ``t_update`` is epoch seconds.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.detector import EIGHTFOLD
from ats.pagination import PageRequest, paginate
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

    def _fetch_page(self, endpoint: str, domain: str, request: PageRequest):
        data = http_client.get_json(endpoint, params={
            "domain": domain, "start": request.offset,
            "num": request.page_size, "sort_by": "timestamp",
        })
        if not isinstance(data, dict):
            raise CollectorUnavailable("Eightfold returned a non-object response")
        return data.get("positions") or [], data.get("count")

    def collect(self) -> CollectionResult:
        host = self._host()
        endpoint = f"https://{host}/api/apply/v2/jobs"
        domain = self._domain()

        try:
            walk = paginate(
                lambda request: self._fetch_page(endpoint, domain, request),
                page_size=PAGE_SIZE, max_jobs=self.max_jobs,
                label=f"{self.company}/eightfold",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"Eightfold API unavailable: {exc}") from exc

        records = [
            self.record(
                title=position.get("name") or position.get("title"),
                location=self._location(position),
                date_posted=position.get("t_create") or position.get("t_update"),
                job_url=position.get("canonicalPositionUrl") or position.get("positionUrl"),
                employment_type=position.get("type"),
                description=position.get("job_description"),
            )
            for position in walk.items
            if isinstance(position, dict)
        ]
        if not records:
            raise CollectorUnavailable("Eightfold API returned zero positions")
        return self.result(walk, records)
