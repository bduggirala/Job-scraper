"""Shared base class for direct-API ATS collectors.

Each collector subclasses :class:`ATSCollector` and implements
:meth:`ATSCollector.collect`, returning a list of normalized records built via
:func:`normalize.build_record`.

Collectors raise :class:`CollectorUnavailable` when they cannot drive the API
for a given tenant (unparseable URL, endpoint gone, empty/hostile response).
That signals the router to fall back to Playwright rather than treating the
company as a hard failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from logger import get_logger
from normalize import build_record, dedupe_records
from settings import load_settings

SCRAPING_METHOD_API = "direct_api"
SCRAPING_METHOD_BROWSER = "playwright"

# -- stop reasons -----------------------------------------------------------
#: The provider served every row it had; pagination ended naturally.
STOP_EXHAUSTED = "exhausted"
#: Collected count reached the total the API reported.
STOP_TOTAL_REACHED = "reported_total_reached"
#: A page beyond the first failed; earlier pages are kept but the walk is short.
STOP_PAGE_FAILED = "page_failed"
#: The collector's own job budget tripped before the provider ran out of rows.
STOP_BUDGET = "budget_exhausted"
#: A page returned rows we had already seen - the usual end-of-results marker.
STOP_NO_NEW_ROWS = "no_new_rows"


@dataclass
class CollectionResult:
    """What a collector retrieved, and whether that is all of it.

    The whole point of this type is ``complete``. A collector that fetched 3 of
    25 pages and one that fetched all 25 used to return the same bare list, so
    no caller could distinguish them - and ``pipeline.run()`` would then delete
    every stored job absent from the partial harvest, reading a transient HTTP
    error as "these jobs all closed".

    ``complete`` is True only when the collector is confident it saw every row
    the provider was willing to serve. Anything else - a failed page, a tripped
    budget, a short walk against a larger reported total - must set it False and
    name a ``stop_reason``.
    """

    jobs: list[dict] = field(default_factory=list)
    complete: bool = True
    pages_fetched: int = 0
    reported_total: int | None = None
    stop_reason: str | None = None

    @property
    def shortfall(self) -> int:
        """How many rows the provider claimed that we did not collect.

        Zero when no total was reported, and never negative: some tenants
        report a stale total lower than the rows they actually serve.
        """
        if self.reported_total is None:
            return 0
        return max(0, int(self.reported_total) - len(self.jobs))

    @classmethod
    def coerce(cls, value: Any) -> "CollectionResult":
        """Wrap a collector's return value, whatever shape it arrived in.

        Migration shim: a collector not yet converted still returns a bare
        list, which is taken at face value as a complete harvest. That
        preserves today's behaviour exactly for unconverted collectors rather
        than silently marking them incomplete.
        """
        if isinstance(value, cls):
            return value
        return cls(jobs=list(value or []))


class CollectorUnavailable(Exception):
    """The direct API cannot serve this company; fall back to Playwright."""


class CollectorError(Exception):
    """The direct API failed in a way that should be recorded as a failure."""


class ATSCollector:
    """Base class for a direct-API ATS collector."""

    #: Canonical provider name, set by each subclass.
    provider: str = "unknown"

    def __init__(self, company: str, detection: dict[str, Any]):
        self.company = company
        self.detection = detection
        self.url = detection.get("url")
        self.host = detection.get("host")
        self.tenant = detection.get("tenant")
        self.site = detection.get("site")
        self.identifier = detection.get("identifier")
        self.settings = load_settings()
        self.log = get_logger(f"ats.{self.provider}")

    # -- helpers ----------------------------------------------------------
    @property
    def max_pages(self) -> int:
        """Legacy page budget, still used by collectors not yet converted.

        Prefer :attr:`max_jobs`: a shared *page* count means wildly different
        job ceilings per collector (25 pages is 250 jobs on Phenom and 5,000 on
        Oracle), which is what silently truncated 23 companies.
        """
        return int(self.settings.get("requests.max_pages_per_company", 25))

    @property
    def max_jobs(self) -> int:
        """How many jobs this collector may collect for one company.

        Expressed in jobs rather than pages so the ceiling means the same thing
        regardless of a provider's page size. Tripping it is not an error, but
        it does make the result incomplete (:data:`STOP_BUDGET`).
        """
        return int(self.settings.get("requests.max_jobs_per_company", 10000))

    def record(self, **kwargs: Any) -> dict[str, Any] | None:
        """Build a normalized record stamped with this collector's provider."""
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("ats_provider", self.provider)
        kwargs.setdefault("scraping_method", SCRAPING_METHOD_API)
        return build_record(**kwargs)

    @staticmethod
    def finalize(records: list[dict | None]) -> list[dict]:
        """Drop None rows and collapse repeated URLs within one company."""
        return dedupe_records([r for r in records if r])

    # -- interface --------------------------------------------------------
    def collect(self) -> list[dict]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<{type(self).__name__} company={self.company!r} tenant={self.tenant!r}>"
