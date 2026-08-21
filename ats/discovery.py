"""Find a working ATS URL or job-search page for a company.

Claude-chat-style discovery replaced the missing URLs in config/companies.xlsx
by searching the web and using judgment. Code cannot judge, so this module
substitutes something stricter: it never decides whether a page *looks* like
the right careers site, it drives every candidate through a real collector and
keeps only what actually returns jobs.

Anything it cannot prove is reported as NOT_FOUND for the user to resolve by
hand. A guess written into the workbook is worse than a blank cell - the
hand-curated pass put marketing pages such as infosys.com/careers/ into the
ATS URL column, which routes to a collector that cannot parse them.

Not part of the pipeline: this is an on-demand tool (tools/find_ats_urls.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from ats.detector import UNKNOWN, detect_ats
from ats.router import COLLECTORS
from logger import get_logger

log = get_logger("ats.discovery")

#: Written into the workbook when nothing could be verified. A first-class
#: outcome, not an error - the user resolves these manually.
NOT_FOUND = "NOT FOUND"


@dataclass
class Discovery:
    """What was proven about one company. Defaults mean "nothing found"."""

    company: str
    ats_url: str | None = None
    provider: str | None = None
    jobs_page: str | None = None
    jobs_found: int = 0
    method: str = "none"          # "http" | "browser" | "none"
    note: str = ""


def verify_ats_url(company: str, url: str) -> tuple[int, str]:
    """Drive ``url`` through its real collector.

    Returns ``(jobs_found, note)``. A return of ``0`` means rejected - the
    caller must not write the URL anywhere. This is the only thing that
    promotes a candidate into a result.
    """
    detection = detect_ats(url)
    provider = detection.get("provider", UNKNOWN)

    collector_class = COLLECTORS.get(provider)
    if collector_class is None:
        return 0, f"no collector for provider {provider!r}"

    try:
        jobs = collector_class(company, detection).collect()
    except Exception as exc:
        return 0, f"{provider} collector failed: {exc}"

    count = len(jobs or [])
    if count == 0:
        return 0, f"{provider} collector returned zero jobs"
    return count, f"{provider} API returned {count} jobs"
