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

from typing import Any

from logger import get_logger
from normalize import build_record, dedupe_records
from settings import load_settings

SCRAPING_METHOD_API = "direct_api"
SCRAPING_METHOD_BROWSER = "playwright"


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
        return int(self.settings.get("requests.max_pages_per_company", 25))

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
