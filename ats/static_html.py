"""Generic server-rendered job list, read over a single HTTP GET.

The rung between JSON-LD and the browser. Plenty of careers sites render their
job list straight into the HTML but expose no JSON-LD and match no provider
fingerprint - and without this tier those went to Playwright, the most
expensive path in the pipeline, capped at three workers, for a page one GET
could have read.

Deliberately provider-agnostic: writing a collector per long-tail careers site
does not scale, but recognising the *shape* of a job list does. The link
heuristics already existed in :mod:`ats.html_utils`, serving iCIMS, Avature and
SuccessFactors; this promotes them to a tier of their own.

The failure mode to avoid is inventing jobs out of site navigation. Two things
guard against it: :func:`ats.html_utils.looks_like_job_link` requires a
job-shaped href, and the router applies the same ``hop_good_enough_rows`` floor
it applies to JSON-LD - a thin result is kept as a fallback while the ladder
continues, never accepted as a company's whole job list. That policy lives in
the router precisely so it is stated once rather than per tier.
"""

from __future__ import annotations

import http_client
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.html_utils import extract_job_links

#: Canonical provider name for records this tier emits.
STATIC_HTML = "static_html"


class StaticHTMLCollector(ATSCollector):
    """Harvest a server-rendered job list from any page."""

    provider = STATIC_HTML

    def collect(self) -> CollectionResult:
        if not self.url:
            raise CollectorUnavailable("No URL available for static HTML collection")

        try:
            html_text = http_client.get_text(
                self.url, headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except Exception as exc:
            raise CollectorUnavailable(
                f"Static HTML fetch failed for {self.url}: {exc}"
            ) from exc

        records = [
            self.record(
                title=link["title"],
                location=link.get("location"),
                date_posted=link.get("date_posted"),
                job_url=link["job_url"],
            )
            for link in extract_job_links(html_text, self.url)
        ]
        jobs = self.finalize([r for r in records if r])

        if not jobs:
            raise CollectorUnavailable(f"No job links found at {self.url}")

        # One page, no pagination control to follow: what is here is all this
        # tier can see. Anything larger belongs to a real collector.
        return CollectionResult(jobs=jobs, complete=True, pages_fetched=1)
