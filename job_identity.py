"""Stable job identity, derived from a job's URL.

``job_url`` is not a safe primary key: the URL slug on most ATS platforms is
title-derived, so retitling a posting (e.g. "Data Engineer" -> "Data Engineer
II") changes the URL while the underlying requisition is the same job. This
module extracts the durable identifier each platform actually assigns, so the
database layer can tell "same job, retitled" apart from "genuinely new job"
without ever touching the collectors or the spec'd normalized record shape.

Deliberately kept separate from ``normalize.py``: this is a database-layer
concern (identity for tracking), not part of the canonical job record.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from ats.detector import (
    ASHBY,
    GREENHOUSE,
    LEVER,
    SMARTRECRUITERS,
    WORKDAY,
)
from deduplicate import normalize_company
from normalize import normalize_url

#: Bumped whenever the id format changes, so a database keyed on the old scheme
#: is cleared rather than silently accumulating two generations of ids that can
#: never match. Version 2 added the company scope prefix; version 3 stopped
#: discarding the query string (which is the entire identity on several
#: platforms) and dropped the provider label from generically-extracted ids.
JOB_ID_SCHEME_VERSION = 3

# Trailing Workday requisition id, e.g. "..._R246063-2" -> "R246063-2".
_WORKDAY_REQ_RE = re.compile(r"_([Rr]\d+(?:-\d+)?)(?:/|$)")

# Generic query-param identifiers used by several enterprise ATS platforms.
# ``job`` is Taleo's (``jobdetail.ftl?job=12345``), ``key`` is Infor's
# (``shorturl.do?key=ZB0``), ``params`` is the opaque blob TEKsystems encodes a
# whole posting into - all three are the only thing distinguishing one of that
# platform's postings from another. ``gh_jid`` is the same story on a Greenhouse
# board configured to point at the employer's own careers page: ISNetworld
# serves every posting at one path and varies only that parameter. It is reached
# only when the path carries no id of its own, because
# ``_PROVIDER_STRATEGIES[GREENHOUSE]`` reads the path first.
_QUERY_ID_KEYS = (
    "jobid", "reqid", "requisitionid", "jobseqno", "opportunityid",
    "job", "id", "key", "params", "gh_jid",
)

# A bare trailing numeric or UUID path segment.
_TRAILING_NUMERIC_RE = re.compile(r"(\d{4,})/?$")
_TRAILING_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", re.I
)


def _path_segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def _query_id(url: str) -> str | None:
    query = urlsplit(url).query
    params = {k.lower(): v for k, v in parse_qsl(query, keep_blank_values=False)}
    for key in _QUERY_ID_KEYS:
        if key in params and params[key]:
            return params[key]
    return None


def _workday_id(url: str) -> str | None:
    match = _WORKDAY_REQ_RE.search(url)
    return match.group(1) if match else None


def _last_segment_id(url: str) -> str | None:
    segments = _path_segments(url)
    if not segments:
        return None
    last = segments[-1]
    uuid_match = _TRAILING_UUID_RE.search(last)
    if uuid_match:
        return uuid_match.group(1)
    numeric_match = _TRAILING_NUMERIC_RE.search(last)
    if numeric_match:
        return numeric_match.group(1)
    return None


# provider -> extraction strategy, tried before the generic fallbacks.
_PROVIDER_STRATEGIES = {
    WORKDAY: _workday_id,
    GREENHOUSE: _last_segment_id,
    LEVER: _last_segment_id,
    ASHBY: _last_segment_id,
    SMARTRECRUITERS: _last_segment_id,
}


def extract_stable_job_id(
    job_url: str | None, ats_provider: str | None, company: str | None = None
) -> str:
    """Return a durable, company-scoped identity for a job.

    Order: provider-specific URL pattern -> generic query-param id -> trailing
    numeric/UUID path segment -> the normalized URL itself. The final fallback
    means every job always gets *some* stable id, even from a custom Playwright
    site where no structured pattern exists - it just degrades to the same
    "same URL = same job" behaviour the pipeline used before this feature.

    Every id is prefixed with a normalized company key. Without that, the
    extracted ids collide across employers: ``ats_provider`` is the literal
    string ``"unknown"`` for every browser-routed company, so
    ``https://a.com/careers?jobId=55512`` and
    ``https://b.com/apply?jobid=55512`` both produced ``unknown:55512`` - and
    since ``job_id`` is the ``jobs`` table primary key, the two employers'
    postings became one row. The later upsert overwrote the earlier company
    name, after which that job silently vanished from the original company's
    set and was treated as removed.

    The company key is normalized (suffixes and punctuation dropped) so
    workbook drift - "Acme Inc" one run, "Acme, Inc." the next - does not
    orphan every job the company had. Renaming a company in the workbook to
    something genuinely different does re-key its jobs, which reports them as
    new once.
    """
    if not job_url:
        return ""

    scope = normalize_company(company) if company else ""
    prefix = f"{scope}:" if scope else ""

    strategy = _PROVIDER_STRATEGIES.get((ats_provider or "").lower())
    if strategy:
        found = strategy(job_url)
        if found:
            return f"{prefix}{ats_provider}:{found}"

    # Opportunistic: some providers (Phenom in particular) surface an
    # applyUrl that actually points at a different underlying ATS - e.g.
    # Chewy's Phenom-branded page applies through Workday. The Workday
    # requisition suffix is distinctive enough (`_R12345`) to check for on
    # any URL with no real false-positive risk, regardless of the labeled
    # provider.
    workday_id = _workday_id(job_url)
    if workday_id:
        return f"{prefix}{WORKDAY}:{workday_id}"

    # Generic extractions carry no provider label. ``ats_provider`` is the
    # literal string "unknown" for every browser-routed row, so labelling a
    # generically-extracted id with it made the *same requisition* resolve to
    # two different identities depending on which route reached it that run -
    # "taleo:1001" via the API, "unknown:1001" via Playwright. A company that
    # fell back to the browser therefore reported its whole job list as new and
    # aged out the API-keyed copies. The company scope already prevents the
    # cross-employer collisions the label was there to stop; within one
    # employer two systems issuing the same numeric id is the residual risk,
    # and it is much smaller than the one being fixed.
    query_id = _query_id(job_url)
    if query_id:
        return f"{prefix}{query_id}"

    segment_id = _last_segment_id(job_url)
    if segment_id:
        return f"{prefix}{segment_id}"

    # The query is kept: on Infor, TEKsystems and similar it is the only thing
    # that differs between two postings, and dropping it gave every job a
    # company lists the same primary key. normalize_url has already stripped
    # the tracking parameters that would otherwise split one job in two.
    return f"{prefix}{normalize_url(job_url) or job_url}"
