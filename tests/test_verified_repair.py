"""Offline tests for pipeline.verified_repair.

Regression coverage for a real bug found in a live full run: JPS Health
Network's ATS URL cell held a dead value, url_repair.py fixed it, and the
repaired page was then further resolved to its real Cornerstone endpoint
(resolved_via_page=True) - but the write-back logic originally excluded
*every* resolved_via_page case, silently leaving the dead ATS URL in place
forever. The exclusion should only apply when the dead value being replaced
lived in the (blank) Live Jobs Page column, where write_discovered_urls
already handles the write-back into the separate ATS URL column instead.
"""

from ats.router import (
    METHOD_API,
    SOURCE_ATS_URL,
    SOURCE_LIVE_PAGE,
    CompanyResult,
    RoutePlan,
)
from pipeline import verified_repair


def _result(**plan_kwargs) -> CompanyResult:
    defaults = dict(
        company="Acme", url=None, provider="unknown", method=METHOD_API,
        source=SOURCE_ATS_URL,
    )
    defaults.update(plan_kwargs)
    plan = RoutePlan(**defaults)
    return CompanyResult(company="Acme", jobs=[{"title": "x"}], plan=plan, success=True)


def test_pure_repair_with_no_further_resolution_qualifies():
    """NTT DATA pattern: repaired landing page, no further ATS found."""
    result = _result(
        url="https://www.nttdata.com/en-us/careers", source=SOURCE_LIVE_PAGE,
        raw_url="https://careers.nttdata.com/", was_repaired=True,
        resolved_via_page=False,
    )
    assert verified_repair(result) == (
        SOURCE_LIVE_PAGE, "https://careers.nttdata.com/", "https://www.nttdata.com/en-us/careers",
    )


def test_repair_from_blank_live_page_then_resolved_is_skipped():
    """Primoris/Cotality pattern: raw_url came from the blank Live Jobs Page,
    then resolved to a real ATS - write_discovered_urls owns this one."""
    result = _result(
        url="https://prim.wd108.myworkdayjobs.com/Primoris/", source=SOURCE_LIVE_PAGE,
        raw_url="https://careers.prim.com/", was_repaired=True,
        resolved_via_page=True,
    )
    assert verified_repair(result) is None


def test_repair_from_dead_ats_url_then_resolved_still_qualifies():
    """JPS Health Network pattern: the dead value was itself in ATS URL, so
    the improved (resolved) URL belongs in that same column."""
    result = _result(
        url="https://jpshealthnet.csod.com/", source=SOURCE_ATS_URL,
        raw_url="https://www.jobs.jpshealthnet.org/", was_repaired=True,
        resolved_via_page=True,
    )
    assert verified_repair(result) == (
        SOURCE_ATS_URL, "https://www.jobs.jpshealthnet.org/", "https://jpshealthnet.csod.com/",
    )


def test_no_repair_happened_returns_none():
    result = _result(
        url="https://acme.wd1.myworkdayjobs.com/External", source=SOURCE_ATS_URL,
        raw_url="https://acme.wd1.myworkdayjobs.com/External", was_repaired=False,
    )
    assert verified_repair(result) is None


def test_failed_company_never_qualifies():
    result = _result(
        url="https://www.nttdata.com/en-us/careers", source=SOURCE_LIVE_PAGE,
        raw_url="https://careers.nttdata.com/", was_repaired=True,
    )
    result.success = False
    assert verified_repair(result) is None
