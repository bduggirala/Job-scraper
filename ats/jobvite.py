"""Jobvite collector - server-rendered careers landing page on jobs.jobvite.com.

Jobvite hosts a branded careers site at::

    https://jobs.jobvite.com/{tenant}/

where ``{tenant}`` is the company's Jobvite slug (``firstcash-holdings-inc``).
Unlike most vendors the tenant is the *first path segment*, not a subdomain -
every tenant shares the same ``jobs.jobvite.com`` host - so the detector reads
the slug from the path.

The landing page is fully server-rendered: its HTML already contains **every**
open job, grouped into per-category ``<table class="jv-job-list">`` blocks.
Each job is one row::

    <tr>
      <td class="jv-job-list-name"><a href="/{tenant}/job/{id}">Title</a></td>
      <td class="jv-job-list-location">City, State</td>
    </tr>

There is no pagination that hides jobs (the per-category "Show More" only
expands rows already present in the DOM) and no reliable posting date on the
list page, so - as with Radancy - ``date_posted`` is left blank rather than
invented. FirstCash Holdings returns ~380 jobs from this single page.

Some companies (e.g. Tyler Technologies) embed the same Jobvite board inside
their own careers site rather than linking jobs.jobvite.com directly; the
detector's ``jobs.jobvite.com`` body fingerprint plus the embedded-URL
extractor let the resolver recover the tenant in that case.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from ats.html_utils import make_soup
from normalize import clean_text

try:  # Wired into ats/detector.py alongside the other providers.
    from ats.detector import JOBVITE
except ImportError:  # pragma: no cover - detector not yet updated
    JOBVITE = "jobvite"

HOST = "jobs.jobvite.com"

# Browser UA: the bare requests default UA is served an interstitial by
# jobs.jobvite.com, while a browser UA gets the full server-rendered list.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class JobviteCollector(ATSCollector):
    provider = JOBVITE

    def _slug(self) -> str:
        """Tenant slug (first path segment of the careers URL)."""
        if self.tenant:
            return str(self.tenant).strip("/")
        if self.url:
            segments = [s for s in urlsplit(self.url).path.split("/") if s]
            if segments:
                return segments[0]
        raise CollectorUnavailable("No Jobvite tenant slug available")

    def _careers_url(self) -> str:
        return f"https://{HOST}/{self._slug()}/"

    def _fetch(self, url: str) -> str:
        return http_client.get_text(
            url,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    def _parse_rows(self, html_text: str) -> list[dict]:
        soup = make_soup(html_text)
        slug = self._slug()
        job_marker = f"/{slug}/job/"

        rows: list[dict] = []
        for anchor in soup.select("td.jv-job-list-name a[href]"):
            href = anchor.get("href") or ""
            if job_marker not in href:
                continue
            title = clean_text(anchor.get_text(" ", strip=True))
            if not title:
                continue

            job_url = f"https://{HOST}{href}" if href.startswith("/") else href
            row = anchor.find_parent("tr")
            rows.append(
                self.record(
                    title=title,
                    location=self._row_location(row),
                    date_posted=None,
                    job_url=job_url,
                )
            )
        return rows

    @staticmethod
    def _row_location(row) -> str | None:
        if row is None:
            return None
        cell = row.find("td", class_="jv-job-list-location")
        if cell is None:
            return None
        return clean_text(cell.get_text(" ", strip=True))

    def collect(self) -> list[dict]:
        careers_url = self._careers_url()
        try:
            html_text = self._fetch(careers_url)
        except Exception as exc:
            raise CollectorUnavailable(
                f"Jobvite careers page unavailable: {exc}"
            ) from exc

        records = self._parse_rows(html_text)
        if not records:
            raise CollectorUnavailable(
                f"Jobvite careers page returned zero jobs for {self._slug()!r}"
            )
        return self.finalize(records)
