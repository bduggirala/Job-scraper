"""Deduplication - scoped strictly to this pipeline.

This module never sees, reads, or merges JobSpy output. Duplicates are
collapsed only among records collected by the company ATS scraper itself.

Two passes run in order:

1. **URL identity** - the same normalized ``job_url`` is the same posting,
   even if the company or title strings differ slightly between sources.
2. **Requisition identity** - the same ``job_id`` is the same posting even
   under a different URL. This is what catches one requisition reached by two
   routes (an ATS API and a branded careers page, or two search queries) whose
   links differ but whose underlying id does not.

The second pass used to key on ``company | title | location | job_url``, which
could never collapse anything the first pass had not already caught: the URL
was one of the four components, so "different URL" and "all four agree" were
contradictory. ``job_id`` is the identity that actually differs from the URL,
it is computed before this stage, and it is company-scoped - so this is both
safe and the thing the composite key was reaching for.

Titles are never merged on their own. ``company | title | location`` without a
stable id would collapse genuinely distinct openings - two Data Engineer roles
on different teams in the same city are two jobs.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from normalize import normalize_url

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Suffixes that differ between listings for the same employer.
# Single tokens only: normalize_company splits on whitespace and filters
# token by token, so a multi-word entry here could never match anything.
_COMPANY_SUFFIXES = (
    "inc", "incorporated", "llc", "ltd", "limited", "corp",
    "corporation", "co", "company", "plc", "lp", "llp", "holdings", "group",
    "the",
)


def _normalize_token(value: Any) -> str:
    if value is None:
        return ""
    text = _PUNCT.sub(" ", str(value).lower())
    return _WS.sub(" ", text).strip()


def normalize_company(value: Any) -> str:
    """Normalize a company name for key comparison."""
    tokens = [t for t in _normalize_token(value).split() if t not in _COMPANY_SUFFIXES]
    return " ".join(tokens)


def normalize_title(value: Any) -> str:
    """Normalize a job title for key comparison."""
    return _normalize_token(value)


def normalize_location(value: Any) -> str:
    """Normalize a location string for key comparison."""
    text = _normalize_token(value)
    # "Dallas TX US" and "Dallas TX United States" should agree.
    text = re.sub(r"\b(united states|usa|us)\b", "", text)
    return _WS.sub(" ", text).strip()


def duplicate_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    """Build the composite duplicate key for a record."""
    return (
        normalize_company(record.get("company")),
        normalize_title(record.get("title")),
        normalize_location(record.get("location")),
        normalize_url(record.get("job_url"), drop_query=True) or "",
    )


def _record_quality(record: dict[str, Any]) -> tuple[int, int, int]:
    """Rank records so the most informative copy survives deduplication."""
    return (
        1 if record.get("date_posted") else 0,
        1 if record.get("description") else 0,
        1 if record.get("scraping_method") == "direct_api" else 0,
    )


def deduplicate(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Collapse duplicates within this pipeline's own results.

    Returns:
        ``{"jobs": [...], "removed": int}``. When two records collide, the one
        carrying more information (real date, description, direct-API origin)
        is kept.
    """
    all_records = list(records)
    input_count = len(all_records)

    by_url: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []

    for record in all_records:
        # The query is KEPT here. On several enterprise platforms it is the
        # only thing that distinguishes one posting from another - UKG serves
        # ``OpportunityDetail?opportunityId=<uuid>``, Taleo
        # ``jobdetail.ftl?job=<id>``, Infor ``shorturl.do?key=<id>`` - so every
        # job a company lists shares one path. Dropping the query collapsed all
        # of them into a single row: measured against a real full run,
        # GameStop's 5,148 distinct postings became 1, BAE Systems' 1,858
        # became 1, and 8,423 real postings were destroyed across 18 companies.
        #
        # Tracking parameters, the thing dropping the query was reaching for,
        # are already removed by normalize_url itself (see TRACKING_PARAMS), so
        # two links differing only by campaign still collapse - and a job whose
        # URL genuinely varies by some other parameter is caught by the
        # job_id pass below.
        url_key = normalize_url(record.get("job_url")) or ""
        if not url_key:
            # No URL to key on: fall back to the composite key alone.
            url_key = "|".join(duplicate_key(record))

        existing = by_url.get(url_key)
        if existing is None:
            by_url[url_key] = record
            ordered.append(url_key)
        elif _record_quality(record) > _record_quality(existing):
            by_url[url_key] = record

    url_unique = [by_url[key] for key in ordered]

    # Second pass on requisition identity: the same job_id under two different
    # URLs is one posting. Rows without a job_id fall back to their composite
    # key, which for them is simply "leave it alone".
    by_identity: dict[str, dict[str, Any]] = {}
    identity_order: list[str] = []

    for record in url_unique:
        key = record.get("job_id") or "|".join(duplicate_key(record))
        existing = by_identity.get(key)
        if existing is None:
            by_identity[key] = record
            identity_order.append(key)
        elif _record_quality(record) > _record_quality(existing):
            by_identity[key] = record

    final = [by_identity[key] for key in identity_order]
    return {"jobs": final, "removed": input_count - len(final)}
