"""Phenom People collector.

Phenom-hosted career sites embed their job data in a JavaScript object on the
search-results page rather than exposing a REST endpoint::

    GET https://{host}/{locale}/search-results?from={n}&s=1
    ...
    <script>phApp.ddo = {"eagerLoadRefineSearch": {"totalHits": N,
                                                   "data": {"jobs": [...]}}};</script>

The rows carry a real ISO ``postedDate``, ``cityStateCountry`` and an
``applyUrl``, which is everything the pipeline needs. Pagination steps ``from``
by the page size until ``totalHits`` is reached.

(The ``/widgets?ddoKey=refineSearch`` endpoint that older Phenom tenants
exposed now 404s on current sites, so it is not used.)
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import (
    STOP_BUDGET,
    STOP_EXHAUSTED,
    STOP_NO_NEW_ROWS,
    STOP_PAGE_FAILED,
    STOP_TOTAL_REACHED,
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
from ats.detector import PHENOM
from normalize import join_location

# Phenom serves 10 rows per request, so the old shared 25-*page* budget capped
# every tenant at 250 jobs - measured live against seven of them (RTX, Cisco,
# HPE, Humana, BCG, Collins Aerospace, Cencora all returned exactly 250). The
# ceiling is now expressed in jobs via ATSCollector.max_jobs, which means the
# same thing regardless of a provider's page size.
PAGE_SIZE = 10

_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*(\{.*?\})\s*;", re.S)


#: Phenom's own ceiling, below the global default. Every page is a full HTML
#: render returning only 10 rows, so the shared 10,000-job budget would mean
#: 1,000 sequential page loads for one company - past the per-company timeout
#: even before per-host pacing. 2,000 is 8x the old 250-job cap while keeping
#: the request count to ~200. A tenant larger than this reports incomplete,
#: which is honest and visible rather than silent.
MAX_JOBS = 2000


class PhenomCollector(ATSCollector):
    provider = PHENOM

    @property
    def max_jobs(self) -> int:
        return min(super().max_jobs, MAX_JOBS)

    def _base_url(self) -> str:
        """Career-site root including its locale segment (e.g. /us/en)."""
        if not self.url:
            if self.host:
                return f"https://{self.host}"
            raise CollectorUnavailable("No Phenom URL available")

        parts = urlsplit(self.url)
        if not parts.netloc:
            raise CollectorUnavailable("Unparseable Phenom URL")

        segments = [s for s in parts.path.split("/") if s]
        # Keep the leading locale prefix and drop deeper paths. Phenom sites
        # use both country codes (/us/en) and region words (/global/en on
        # careers.rtx.com) - matching only two-letter codes dropped the
        # /global part and requested /search-results from the wrong root,
        # which is why RTX, Collins Aerospace and BCG all returned nothing.
        locale: list[str] = []
        for segment in segments[:2]:
            if re.fullmatch(r"[a-z]{2}|global|intl|international", segment, re.I):
                locale.append(segment)
            else:
                break

        path = "/" + "/".join(locale) if locale else ""
        return f"https://{parts.netloc}{path}"

    @staticmethod
    def _parse_ddo(html_text: str) -> dict[str, Any] | None:
        match = _DDO_RE.search(html_text)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _extract_jobs(ddo: dict[str, Any]) -> tuple[list[dict], int | None]:
        """Pull the job array out of whichever ddo key holds it."""
        for key in ("eagerLoadRefineSearch", "refineSearch"):
            node = ddo.get(key)
            if not isinstance(node, dict):
                continue
            data = node.get("data")
            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                return data["jobs"], node.get("totalHits") or node.get("totalCount")
        return [], None

    def _location(self, job: dict[str, Any]) -> str | None:
        multi = job.get("multi_location_array")
        if isinstance(multi, list) and len(multi) > 1:
            names = [
                m.get("city") if isinstance(m, dict) else str(m)
                for m in multi[:6]
            ]
            joined = join_location(*[n for n in names if n], separator=" | ")
            if joined:
                return joined

        for key in ("cityStateCountry", "cityState", "location", "address"):
            value = job.get(key)
            if value:
                return str(value)
        return join_location(job.get("city"), job.get("state"), job.get("country"))

    def _job_url(self, base: str, job: dict[str, Any]) -> str | None:
        apply_url = job.get("applyUrl")
        if apply_url and str(apply_url).startswith("http"):
            return str(apply_url)

        seq = job.get("jobSeqNo") or job.get("jobId")
        if seq:
            return f"{base}/job/{seq}"
        return str(apply_url) if apply_url else None

    def collect(self) -> CollectionResult:
        base = self._base_url()
        search_url = f"{base}/search-results"

        records: list[dict | None] = []
        seen: set[str] = set()
        page = 0
        total: int | None = None
        stop_reason = STOP_EXHAUSTED
        complete = True

        while len(records) < self.max_jobs:
            offset = page * PAGE_SIZE
            try:
                html_text = http_client.get_text(
                    search_url,
                    params={"from": offset, "s": "1"},
                    headers={"Accept": "text/html", "Referer": base},
                )
            except Exception as exc:
                if page == 0:
                    raise CollectorUnavailable(f"Phenom search-results unavailable: {exc}") from exc
                self.log.warning(
                    "%s: Phenom page %s failed (%s); marking incomplete",
                    self.company, page, exc,
                )
                complete, stop_reason = False, STOP_PAGE_FAILED
                break

            ddo = self._parse_ddo(html_text)
            if ddo is None:
                if page == 0:
                    raise CollectorUnavailable("Phenom page did not contain phApp.ddo")
                complete, stop_reason = False, STOP_PAGE_FAILED
                break
            page += 1

            jobs, page_total = self._extract_jobs(ddo)
            if total is None and page_total is not None:
                total = int(page_total)
            if not jobs:
                break

            page_records = [
                self.record(
                    title=job.get("title"),
                    location=self._location(job),
                    date_posted=job.get("postedDate") or job.get("dateCreated"),
                    job_url=self._job_url(base, job),
                    apply_url=job.get("applyUrl"),
                    employment_type=job.get("type"),
                    description=job.get("descriptionTeaser"),
                )
                for job in jobs
                if isinstance(job, dict)
            ]

            fresh = [r for r in page_records if r and r["job_url"] not in seen]
            if not fresh:
                stop_reason = STOP_NO_NEW_ROWS
                break
            for record in fresh:
                seen.add(record["job_url"])
            records.extend(fresh)

            if total is not None and len(records) >= int(total):
                stop_reason = STOP_TOTAL_REACHED
                break
        else:
            complete, stop_reason = False, STOP_BUDGET

        if not records:
            raise CollectorUnavailable("Phenom search-results returned zero jobs")

        jobs = self.finalize(records)
        if not complete:
            self.log.warning(
                "%s: Phenom scrape INCOMPLETE (%s) - collected %s of %s",
                self.company, stop_reason, len(jobs), total,
            )
        return CollectionResult(
            jobs=jobs, complete=complete, pages_fetched=page,
            reported_total=total, stop_reason=stop_reason,
        )
