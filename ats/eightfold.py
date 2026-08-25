"""Eightfold AI collector - public apply API.

    GET https://{host}/api/apply/v2/jobs?domain={domain}&start=0&num=50

Eightfold powers many branded career sites (the page is a thin shell over this
endpoint). ``t_update`` is epoch seconds.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_PAGE_FAILED,
    STOP_TOTAL_REACHED,
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
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

    def collect(self) -> CollectionResult:
        host = self._host()
        endpoint = f"https://{host}/api/apply/v2/jobs"
        domain = self._domain()

        records: list[dict | None] = []
        start = 0
        pages = 0
        total: int | None = None
        stop_reason = STOP_EXHAUSTED
        complete = True

        while len(records) < self.max_jobs:
            params = {
                "domain": domain,
                "start": start,
                "num": PAGE_SIZE,
                "sort_by": "timestamp",
            }
            try:
                data = http_client.get_json(endpoint, params=params)
            except Exception as exc:
                if pages == 0:
                    raise CollectorUnavailable(f"Eightfold API unavailable: {exc}") from exc
                self.log.warning(
                    "%s: Eightfold page %s failed (%s); marking incomplete",
                    self.company, pages, exc,
                )
                complete, stop_reason = False, STOP_PAGE_FAILED
                break

            if not isinstance(data, dict):
                raise CollectorUnavailable("Eightfold returned a non-object response")

            positions = data.get("positions") or []
            if total is None:
                total = data.get("count")
            if not positions:
                break
            pages += 1

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
                stop_reason = STOP_TOTAL_REACHED
                break
        else:
            complete, stop_reason = False, STOP_BUDGET

        if not records:
            raise CollectorUnavailable("Eightfold API returned zero positions")

        jobs = self.finalize(records)
        if not complete:
            self.log.warning(
                "%s: Eightfold scrape INCOMPLETE (%s) - collected %s of %s",
                self.company, stop_reason, len(jobs), total,
            )
        return CollectionResult(
            jobs=jobs, complete=complete, pages_fetched=pages,
            reported_total=total, stop_reason=stop_reason,
        )
