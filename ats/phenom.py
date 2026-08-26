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
    STOP_BUDGET_UNORDERED,
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
)
from ats.pagination import PageRequest, paginate
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
#: 1,000 sequential page loads for one company.
#:
#: 8,000 is ~800 requests, about 507s at the configured 3 req/s - inside the
#: 900s per-company budget with room to spare. That is enough to complete
#: Collins Aerospace, RTX and Humana, which a 2,000 ceiling truncated. CVS
#: Health (18,904 postings) and Signify stay incomplete and always will: 1,890
#: sequential requests is ~1,020s at the rate those two actually achieve
#: (8,000 rows took 432s each, sharing one rate-limited host), and overrunning
#: the per-company budget loses the company outright rather than truncating it.
#:
#: **The gap is not the oldest postings.** That was claimed here for a long
#: time, on the strength of the ``s=1`` parameter the search URL carries, and
#: it is false: offset 0 returned a 12 June posting while offset 7,990 returned
#: one from 24 August, and none of ``s=2``/``s=3``/``sortBy``/``keywords``/``q``
#: changed the ordering or the total - this endpoint ignores them all. The rows
#: beyond the ceiling are an arbitrary slice by date, so a truncated Phenom
#: tenant may be hiding jobs posted today. Hence STOP_BUDGET_UNORDERED, which
#: keeps these companies out of the "we know what we missed" set.
MAX_JOBS = 8000


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

    def _page(self, search_url: str, base: str, request: PageRequest):
        html_text = http_client.get_text(
            search_url,
            params={"from": request.offset, "s": "1"},
            headers={"Accept": "text/html", "Referer": base},
        )
        ddo = self._parse_ddo(html_text)
        if ddo is None:
            raise CollectorUnavailable("Phenom page did not contain phApp.ddo")
        jobs, page_total = self._extract_jobs(ddo)
        rows = [
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
        return [r for r in rows if r], page_total

    def collect(self) -> CollectionResult:
        base = self._base_url()
        search_url = f"{base}/search-results"

        try:
            walk = paginate(
                lambda request: self._page(search_url, base, request),
                page_size=PAGE_SIZE, max_jobs=self.max_jobs,
                key=lambda row: row["job_url"],
                label=f"{self.company}/phenom",
            )
        except CollectorUnavailable:
            raise
        except Exception as exc:
            raise CollectorUnavailable(f"Phenom search-results unavailable: {exc}") from exc

        if not walk.items:
            raise CollectorUnavailable("Phenom search-results returned zero jobs")

        result = self.result(walk, walk.items)
        if result.stop_reason == STOP_BUDGET:
            # Phenom serves by relevance, not date (see the note on MAX_JOBS),
            # so this ceiling hides postings of every age rather than only the
            # oldest. Saying so is what keeps these companies out of the set a
            # digest is allowed to treat as fully understood.
            self.log.warning(
                "%s: Phenom ceiling reached at %s of %s postings - the rows "
                "beyond it are NOT the oldest, this tenant serves by relevance",
                self.company, len(result.jobs), result.reported_total,
            )
            result.stop_reason = STOP_BUDGET_UNORDERED
        return result
