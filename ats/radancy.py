"""Radancy TalentBrew collector - server-rendered search on the company domain.

Radancy TalentBrew (formerly TMP Worldwide) hosts a branded careers site on the
*company's own* domain (``careers.7-eleven.com``) rather than a vendor host, so
:func:`ats.detector.detect_ats` cannot recognise it from the URL - only the HTML
fingerprints in ``BODY_FINGERPRINTS`` identify it, exactly as for Phenom on a
branded domain.

Its job list is served from a stable results endpoint::

    GET https://{host}/search-jobs/results?CurrentPage={n}&RecordsPerPage={rpp}&Keyword=...

which returns JSON ``{"results": "<html job cards>", "hasJobs": bool, ...}``. Each
card is an ``<a data-job-id href="/job/{loc}/{slug}/{org}/{id}"><h2>Title</h2>``
with a nearby location span. There is no reliable posting date on the list page,
so ``date_posted`` is left blank and the freshness filter flags it rather than
inventing one. Pagination walks ``CurrentPage`` until a page yields no new ids.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.detector import RADANCY
from ats.html_utils import make_soup
from normalize import clean_text

# Large page size keeps the request count low: 7-Eleven's ~5,100 jobs come back
# in ~11 requests at 500/page rather than ~52 at 100. Bounded by MAX_PAGES so a
# tenant that ignores pagination cannot spin forever.
RECORDS_PER_PAGE = 500
MAX_PAGES = 60

# Strips the visible field label Radancy renders inside the location span
# (``<b>Location</b> Mattydale, NY`` -> ``Mattydale, NY``).
_LABEL_RE = re.compile(r"^(job\s+)?locations?\s*[:\-]?\s*", re.I)


class RadancyCollector(ATSCollector):
    provider = RADANCY

    def _base_host(self) -> str:
        if self.host:
            return self.host
        if self.url:
            return urlsplit(self.url if "//" in self.url else f"https://{self.url}").netloc
        raise CollectorUnavailable("No Radancy host available")

    def _fetch_page(self, results_url: str, page: int) -> str:
        params = {
            "CurrentPage": page,
            "RecordsPerPage": RECORDS_PER_PAGE,
            "ActiveFacetID": 0,
            "Distance": 50,
            "RadiusUnitType": 0,
            "Keyword": "",
            "Location": "",
            "ShowRadius": "False",
            "IsPagination": "True",
            "SearchResultsModuleName": "Search Results",
            "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": 0,
            "SortDirection": 0,
            "SearchType": 5,
            "ResultsType": 0,
        }
        body = http_client.get_text(
            results_url,
            params=params,
            headers={
                "Accept": "application/json, text/javascript, */*",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        # The endpoint returns JSON with an HTML ``results`` fragment, but some
        # tenants answer with the raw fragment - accept either.
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return body
        if isinstance(data, dict):
            return data.get("results") or ""
        return ""

    def _parse_cards(self, fragment: str, base: str) -> list[dict]:
        soup = make_soup(fragment)
        rows: list[dict] = []
        for anchor in soup.select("a[data-job-id][href]"):
            href = anchor.get("href") or ""
            if "/job/" not in href.lower():
                continue
            heading = anchor.find(["h2", "h3"])
            title = clean_text(heading.get_text(" ", strip=True)) if heading else None
            if not title:
                continue
            rows.append(
                self.record(
                    title=title,
                    location=self._card_location(anchor),
                    date_posted=None,
                    job_url=f"https://{self._base_host()}{href}" if href.startswith("/") else href,
                )
            )
        return rows

    @staticmethod
    def _card_location(anchor) -> str | None:
        node = anchor.find(attrs={"class": re.compile("job-location", re.I)})
        if node is None:
            node = anchor.find(attrs={"class": re.compile("job-info", re.I)})
        if node is None:
            return None
        text = clean_text(node.get_text(" ", strip=True))
        if not text:
            return None
        return _LABEL_RE.sub("", text) or None

    def collect(self) -> list[dict]:
        host = self._base_host()
        results_url = f"https://{host}/search-jobs/results"

        records: list[dict | None] = []
        seen: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            try:
                fragment = self._fetch_page(results_url, page)
            except Exception as exc:
                if page == 1:
                    raise CollectorUnavailable(
                        f"Radancy results endpoint unavailable: {exc}"
                    ) from exc
                break

            fresh = [
                row for row in self._parse_cards(fragment, results_url)
                if row and row["job_url"] not in seen
            ]
            if not fresh:
                break
            for row in fresh:
                seen.add(row["job_url"])
            records.extend(fresh)

        if not records:
            raise CollectorUnavailable("Radancy results endpoint returned zero jobs")
        return self.finalize(records)
