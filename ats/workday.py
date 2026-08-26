"""Workday collector - CXS jobs endpoint.

Workday exposes a stable, unauthenticated JSON search endpoint behind every
public career site::

    POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

The response carries ``total`` and a ``jobPostings`` array. ``limit`` is capped
at 20 server-side, so results are paged with ``offset``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import http_client
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.detector import WORKDAY
from ats.pagination import PageRequest, paginate
from normalize import join_location

PAGE_SIZE = 20

#: A ``bulletFields`` entry that is a requisition id rather than a location:
#: an optional short letter prefix, then digits and separators only. Observed
#: live as R999094, 26031220, JR0148744, 1645097, R00352812, JREQ201133.
#: A location token ("Heredia", "New South Wales") never takes this shape.
_REQ_ID_RE = re.compile(r"^[A-Za-z]{0,6}[-_]?\d[\d\-_]*$")


class WorkdayCollector(ATSCollector):
    provider = WORKDAY

    def _endpoint(self) -> str:
        if not self.host or not self.tenant or not self.site:
            raise CollectorUnavailable(
                f"Incomplete Workday coordinates (host={self.host}, "
                f"tenant={self.tenant}, site={self.site})"
            )
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    def _base_site_url(self) -> str:
        """Public site root used to turn externalPath into an absolute URL."""
        parts = urlsplit(self.url or f"https://{self.host}")
        segments = [s for s in parts.path.split("/") if s]

        locale = "en-US"
        for segment in segments:
            if re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", segment):
                locale = segment
                break
        return f"https://{self.host}/{locale}/{self.site}"

    @staticmethod
    def _location_from_bullets(posting: dict[str, Any]) -> str | None:
        """Location from ``bulletFields``, for tenants sending no locationsText.

        Some tenants omit ``locationsText`` and ``locations`` entirely and put
        the location in ``bulletFields`` alongside the requisition id. Reading
        only the first two fields left every posting from those employers with
        a blank location, which the DFW filter drops - so no job from Accenture
        (1,130 postings) or Thomson Reuters (468) could match, wherever it was.

        The split is clean: a tenant that sends ``locationsText`` puts *only*
        the requisition id in ``bulletFields``, so this can only fire where
        there is nothing to lose. The id to discard is the token
        ``externalPath`` already ends with (``..._R00352812``), which makes the
        exclusion exact rather than a guess at what a job id looks like; the
        digits-dominated check behind it covers a tenant whose path does not
        carry one.
        """
        bullets = posting.get("bulletFields")
        if not isinstance(bullets, list):
            return None

        path = str(posting.get("externalPath") or "")
        req_id = path.rsplit("_", 1)[-1] if "_" in path else ""

        parts = []
        for bullet in bullets:
            text = str(bullet or "").strip()
            if not text:
                continue
            # "R999094" against an externalPath ending "_R999094-2", and the
            # reverse, are the same requisition.
            if req_id and (text == req_id or req_id.startswith(text)
                           or text.startswith(req_id)):
                continue
            if _REQ_ID_RE.match(text):
                continue
            parts.append(text)

        return join_location(*parts) if parts else None

    def _job_url(self, external_path: str | None) -> str | None:
        if not external_path:
            return None
        if external_path.startswith("http"):
            return external_path
        return urljoin(self._base_site_url().rstrip("/") + "/", external_path.lstrip("/"))

    @staticmethod
    def _extract_posted(posting: dict[str, Any]) -> Any:
        """Workday puts the date in postedOn, or in a bulletFields entry."""
        for key in ("postedOn", "startDate", "postedOnDate"):
            value = posting.get(key)
            if value:
                return value

        for bullet in posting.get("bulletFields") or []:
            if isinstance(bullet, str) and re.search(r"posted|ago|today|yesterday", bullet, re.I):
                return bullet
        return None

    def _fetch_page(self, endpoint: str, request: PageRequest):
        # No sort parameter: Workday CXS ignores one. Verified directly against
        # Capital One's tenant - requesting sortBy=POSTING_DATES_DESC returns a
        # byte-identical first page to sending nothing, so passing it would be
        # a dead parameter that reads as though ordering were guaranteed.
        #
        # The default order is already posting-date descending, which is what
        # a truncated walk needs. Measured over that tenant's 1,854 postings:
        # mean age climbs monotonically from 1.6 days in rows 0-200 to 29.0
        # days in the last 200. So a budget-truncated Workday walk keeps the
        # freshest requisitions - the ones a 7-day window can still match.
        payload = {
            "appliedFacets": {},
            "limit": request.page_size,
            "offset": request.offset,
            "searchText": "",
        }
        data = http_client.post_json(
            endpoint, payload,
            headers={"Accept": "application/json", "Referer": self._base_site_url()},
        )
        if not isinstance(data, dict):
            raise CollectorUnavailable("Workday CXS returned a non-object response")
        return data.get("jobPostings") or [], data.get("total")

    def collect(self) -> CollectionResult:
        endpoint = self._endpoint()

        try:
            walk = paginate(
                lambda request: self._fetch_page(endpoint, request),
                page_size=PAGE_SIZE,
                max_jobs=self.max_jobs,
                label=f"{self.company}/workday",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"Workday CXS unavailable: {exc}") from exc

        records = [
            self.record(
                title=posting.get("title"),
                location=(posting.get("locationsText") or posting.get("locations")
                          or self._location_from_bullets(posting)),
                date_posted=self._extract_posted(posting),
                job_url=self._job_url(posting.get("externalPath")),
                employment_type=posting.get("timeType"),
                description=posting.get("jobDescription"),
            )
            for posting in walk.items
            if isinstance(posting, dict)
        ]
        if not records:
            raise CollectorUnavailable("Workday CXS returned zero postings")

        return self.result(walk, records)
