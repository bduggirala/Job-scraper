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
from normalize import normalize_url

# Trailing Workday requisition id, e.g. "..._R246063-2" -> "R246063-2".
_WORKDAY_REQ_RE = re.compile(r"_([Rr]\d+(?:-\d+)?)(?:/|$)")

# Generic query-param identifiers used by several enterprise ATS platforms.
_QUERY_ID_KEYS = ("id", "jobid", "reqid", "requisitionid", "jobseqno", "opportunityid")

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


def extract_stable_job_id(job_url: str | None, ats_provider: str | None) -> str:
    """Return a durable identity for a job, best available.

    Order: provider-specific URL pattern -> generic query-param id -> trailing
    numeric/UUID path segment -> the normalized URL itself. The final fallback
    means every job always gets *some* stable id, even from a custom Playwright
    site where no structured pattern exists - it just degrades to the same
    "same URL = same job" behaviour the pipeline used before this feature.
    """
    if not job_url:
        return ""

    strategy = _PROVIDER_STRATEGIES.get((ats_provider or "").lower())
    if strategy:
        found = strategy(job_url)
        if found:
            return f"{ats_provider}:{found}"

    # Opportunistic: some providers (Phenom in particular) surface an
    # applyUrl that actually points at a different underlying ATS - e.g.
    # Chewy's Phenom-branded page applies through Workday. The Workday
    # requisition suffix is distinctive enough (`_R12345`) to check for on
    # any URL with no real false-positive risk, regardless of the labeled
    # provider.
    workday_id = _workday_id(job_url)
    if workday_id:
        return f"{WORKDAY}:{workday_id}"

    query_id = _query_id(job_url)
    if query_id:
        return f"{ats_provider}:{query_id}"

    segment_id = _last_segment_id(job_url)
    if segment_id:
        return f"{ats_provider}:{segment_id}"

    return normalize_url(job_url, drop_query=True) or job_url
