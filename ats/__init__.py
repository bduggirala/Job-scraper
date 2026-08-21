"""Direct-API ATS collectors and the routing layer.

Public surface:
    detect_ats(url)                 - identify the ATS behind a URL
    plan_route(company, ...)        - decide provider + scraping method
    fetch_company_jobs(company, ...) - scrape one company end to end
"""

from ats.base import ATSCollector, CollectorError, CollectorUnavailable
from ats.detector import SUPPORTED_PROVIDERS, UNKNOWN, detect_ats
from ats.router import (
    COLLECTORS,
    METHOD_API,
    METHOD_BROWSER,
    CompanyResult,
    RoutePlan,
    fetch_company_jobs,
    plan_route,
)

__all__ = [
    "ATSCollector",
    "CollectorError",
    "CollectorUnavailable",
    "COLLECTORS",
    "CompanyResult",
    "METHOD_API",
    "METHOD_BROWSER",
    "RoutePlan",
    "SUPPORTED_PROVIDERS",
    "UNKNOWN",
    "detect_ats",
    "fetch_company_jobs",
    "plan_route",
]
