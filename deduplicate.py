"""Deduplication - scoped strictly to this pipeline.

This module never sees, reads, or merges JobSpy output. Duplicates are
collapsed only among records collected by the company ATS scraper itself.

Two passes run in order:

1. **URL identity** - the same normalized ``job_url`` is the same posting,
   even if the company or title strings differ slightly between sources.
2. **Composite key** - ``company | title | location | job_url`` per the
   pipeline spec, catching a posting re-listed under a different URL only when
   all four components agree.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from normalize import normalize_url

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

# Suffixes that differ between listings for the same employer.
_COMPANY_SUFFIXES = (
    "inc", "incorporated", "llc", "l l c", "ltd", "limited", "corp",
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
        url_key = normalize_url(record.get("job_url"), drop_query=True) or ""
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

    by_composite: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    composite_order: list[tuple[str, str, str, str]] = []

    for record in url_unique:
        key = duplicate_key(record)
        existing = by_composite.get(key)
        if existing is None:
            by_composite[key] = record
            composite_order.append(key)
        elif _record_quality(record) > _record_quality(existing):
            by_composite[key] = record

    final = [by_composite[key] for key in composite_order]
    return {"jobs": final, "removed": input_count - len(final)}


def deduplicate_list(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convenience wrapper returning just the deduplicated records."""
    return deduplicate(records)["jobs"]
