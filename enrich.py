"""Post-filter enrichment for records whose listing data is too coarse.

Some ATS listing endpoints return an aggregate location label instead of real
locations - Workday, for example, reports ``"3 Locations"`` for any
multi-location requisition. That is fatal for a metro-area filter: a job open
in Dallas, McLean and Chicago would never match "Dallas".

Enrichment fetches the per-job detail record to recover the true locations and
an exact posting date. It runs **after** the target-role filter precisely
because it is the expensive stage: a company with 1,800 postings typically has
only a handful of matching roles, so this costs a few requests rather than
thousands.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.detector import WORKDAY
from logger import get_logger
from normalize import join_location

log = get_logger("enrich")

# Workday's aggregate label, e.g. "3 Locations".
AGGREGATE_LOCATION_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def needs_enrichment(record: dict[str, Any]) -> bool:
    """True when a record's location is an aggregate label or missing."""
    if record.get("ats_provider") != WORKDAY:
        return False
    location = record.get("location")
    return not location or bool(AGGREGATE_LOCATION_RE.match(str(location)))


def _workday_detail_url(job_url: str) -> str | None:
    """Map a public Workday job URL to its CXS detail endpoint.

    ``https://{host}/{locale}/{site}/job/{path}``
        -> ``https://{host}/wday/cxs/{tenant}/{site}/job/{path}``
    """
    try:
        parts = urlsplit(job_url)
    except ValueError:
        return None

    host = parts.netloc
    if not host or "myworkday" not in host:
        return None

    tenant = host.split(".")[0]
    segments = [s for s in parts.path.split("/") if s]
    if "job" not in segments:
        return None

    job_index = segments.index("job")
    # Everything before "/job/" ends with the site id.
    before = segments[:job_index]
    if not before:
        return None
    site = before[-1]

    remainder = "/".join(segments[job_index:])
    return f"https://{host}/wday/cxs/{tenant}/{site}/{remainder}"


def _enrich_workday_record(record: dict[str, Any]) -> dict[str, Any]:
    """Fetch a Workday job detail and fill in real location + date."""
    detail_url = _workday_detail_url(record.get("job_url", ""))
    if not detail_url:
        return record

    try:
        payload = http_client.get_json(detail_url, headers={"Accept": "application/json"})
    except Exception as exc:
        log.debug("Workday enrichment failed for %s: %s", record.get("job_url"), exc)
        return record

    info = (payload or {}).get("jobPostingInfo")
    if not isinstance(info, dict):
        return record

    enriched = dict(record)

    primary = info.get("location")
    additional = info.get("additionalLocations")
    if isinstance(additional, list) and additional:
        combined = join_location(primary, *[str(a) for a in additional[:6]], separator=" | ")
    else:
        combined = primary

    if combined:
        enriched["location"] = combined

    # startDate is an exact ISO date; far better than "Posted 8 Days Ago".
    start_date = info.get("startDate")
    if start_date:
        enriched["date_posted"] = str(start_date)

    remote_type = info.get("remoteType")
    if isinstance(remote_type, str) and remote_type:
        enriched["remote"] = "remote" in remote_type.lower()

    if not enriched.get("employment_type") and info.get("timeType"):
        enriched["employment_type"] = info["timeType"]

    return enriched


def enrich_records(
    records: list[dict[str, Any]],
    *,
    workers: int = 8,
) -> list[dict[str, Any]]:
    """Enrich records that need it, leaving the rest untouched.

    Never raises and never drops a record: a failed lookup returns the
    original row, so enrichment can only add information.
    """
    targets = [(i, r) for i, r in enumerate(records) if needs_enrichment(r)]
    if not targets:
        return records

    log.info("Enriching %s record(s) with coarse location data", len(targets))
    enriched = list(records)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enrich") as pool:
        futures = {
            pool.submit(_enrich_workday_record, record): index
            for index, record in targets
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                enriched[index] = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("Enrichment worker failed: %s", exc)

    return enriched
