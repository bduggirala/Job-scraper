"""Normalization helpers shared by every collector.

Every ATS collector and the Playwright fallback must emit records through
:func:`build_record` so downstream stages see one identical shape. Fields that
are genuinely unavailable stay ``None`` - never invented, never defaulted to
empty strings beyond the required identity fields.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dateutil import parser as date_parser

# Canonical field order for the normalized record.
RECORD_FIELDS = (
    "company",
    "title",
    "location",
    "date_posted",
    "job_url",
    "apply_url",
    "employment_type",
    "remote",
    "description",
    "ats_provider",
    "scraping_method",
)

# Query parameters stripped during URL normalization (tracking / attribution).
#
# ``gh_jid`` is deliberately absent, though it looks like it belongs: it is
# Greenhouse's job id, and on a board that points at the employer's own site it
# is the *only* thing distinguishing two postings. ISNetworld serves all 18 of
# its postings as ``isnetworld.com/en/about/careers/jobs?gh_jid=<id>``, so
# stripping it normalized every one of them to the same URL and
# :func:`dedupe_records` collapsed the board to a single job. Tenants whose
# ``absolute_url`` already carries the id in the path (SoFi, and every board
# left on ``job-boards.greenhouse.io``) are unaffected either way - their
# identity comes from the path, which is what
# ``job_identity._PROVIDER_STRATEGIES[GREENHOUSE]`` reads.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gh_src", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
    "src", "source", "ref", "referrer", "referral", "trackingid", "trk",
    "recruiter", "campaign", "cid", "sid", "iis", "iisn", "utm_referrer",
    "applyurl", "from", "jobposition", "cx_source",
}

_WHITESPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")

#: A posting older than this is treated as a parsing artefact, not a real date.
#: Generous on purpose - the point is to catch nonsense like a fuzzy parse
#: landing in 1998, not to second-guess a genuinely long-running requisition.
MAX_AGE_DAYS = 1095  # ~3 years

#: What has to be present before a string is handed to dateutil's fuzzy parser:
#: a month name, or a run of digits separated by / or - (2026-08-20, 8/20/26).
#: Without this guard "Building 7" and "Level 3" both parse as dates.
_LOOKS_LIKE_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\d{1,4}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}:\d{2}",
    re.I,
)

_REMOTE_HINTS = (
    "remote", "work from home", "work-from-home", "wfh", "telecommute",
    "virtual", "anywhere", "home based", "home-based", "distributed",
)
_HYBRID_HINTS = ("hybrid", "flex office", "flexible office")


def clean_text(value: Any) -> str | None:
    """Collapse whitespace and unescape entities. Returns None when empty."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = html.unescape(value)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def strip_html(value: Any, max_length: int | None = 5000) -> str | None:
    """Convert an HTML fragment to plain text, optionally truncated."""
    if value is None:
        return None
    text = _TAG_RE.sub(" ", str(value))
    text = clean_text(text)
    if text and max_length and len(text) > max_length:
        text = text[:max_length].rstrip() + "..."
    return text


def normalize_url(url: Any, *, drop_query: bool = False) -> str | None:
    """Canonicalize a URL: lowercase host, strip tracking params and fragment.

    Used for both output and deduplication so two links to the same posting
    that differ only by campaign parameters collapse to one key.
    """
    if not url:
        return None
    text = str(url).strip()
    if not text:
        return None

    try:
        parts = urlsplit(text)
    except ValueError:
        return text

    if not parts.scheme and not parts.netloc:
        return text

    netloc = parts.netloc.lower()
    # Drop default ports and a leading "www." for stable comparison.
    netloc = re.sub(r":443$", "", re.sub(r":80$", "", netloc))

    path = parts.path.rstrip("/") or "/"

    if drop_query:
        query = ""
    else:
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=False)
            if key.lower() not in TRACKING_PARAMS
        ]
        query = urlencode(sorted(kept))

    return urlunsplit((parts.scheme.lower() or "https", netloc, path, query, ""))


def parse_date(value: Any, *, reference: datetime | None = None) -> datetime | None:
    """Best-effort parse of the many date shapes ATS platforms emit.

    Handles ISO-8601 strings, epoch seconds/milliseconds, and the relative
    phrasing Workday and friends use ("Posted 3 Days Ago", "Posted Today").
    Returns a timezone-aware UTC datetime, or None when nothing reliable can
    be derived - callers must not substitute "now" for an unknown date.
    """
    if value is None or value == "":
        return None

    now = reference or datetime.now(timezone.utc)

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    # Epoch seconds or milliseconds.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        if seconds <= 0:
            return None
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = clean_text(value)
    if not text:
        return None

    lowered = text.lower()

    # Numeric strings that are really epochs.
    if re.fullmatch(r"\d{10,13}", lowered):
        return parse_date(int(lowered), reference=now)

    if "just posted" in lowered or "today" in lowered or "just now" in lowered:
        return now
    if "yesterday" in lowered:
        return now - timedelta(days=1)

    relative = re.search(
        r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month|year)s?\s*(ago|old)?", lowered
    )
    if relative and ("ago" in lowered or "posted" in lowered or "old" in lowered):
        amount = int(relative.group(1))
        unit = relative.group(2)
        deltas = {
            "minute": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
            "month": timedelta(days=30 * amount),
            "year": timedelta(days=365 * amount),
        }
        return now - deltas[unit]

    # Strip common prefixes before handing to dateutil.
    cleaned = re.sub(r"^(posted|published|date\s*posted|posted\s*on)\s*:?\s*", "", lowered).strip()
    if not cleaned:
        return None

    # dateutil's fuzzy mode will pull a "date" out of almost any string
    # containing a number - "Building 7", "Suite 200", "Level 3" and
    # "5 openings" all parse. Browser-scraped text reaches here routinely, so
    # require something that actually looks like a date first.
    if not _LOOKS_LIKE_DATE_RE.search(cleaned):
        return None

    try:
        parsed = date_parser.parse(cleaned, fuzzy=True, default=now.replace(
            hour=0, minute=0, second=0, microsecond=0))
    except (ValueError, OverflowError, TypeError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    # Bounded both ways. A far-future date is nonsense, and so is a posting
    # from years ago - both are parsing artefacts rather than real postings,
    # and an artefact old date is the dangerous one: it classifies a fresh job
    # as stale and drops it silently.
    if parsed > now + timedelta(days=2):
        return None
    if parsed < now - timedelta(days=MAX_AGE_DAYS):
        return None
    return parsed


def format_date(value: datetime | None) -> str | None:
    """Render a datetime as an ISO-8601 UTC string."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def join_location(*parts: Any, separator: str = ", ") -> str | None:
    """Join non-empty location fragments, de-duplicating repeats."""
    seen: list[str] = []
    for part in parts:
        cleaned = clean_text(part)
        if cleaned and cleaned.lower() not in {s.lower() for s in seen}:
            seen.append(cleaned)
    return separator.join(seen) if seen else None


def infer_remote(*candidates: Any) -> bool | None:
    """Infer a remote flag from title/location/description text.

    Returns True/False when there is a signal, and None when there is not -
    an absent signal is not evidence the role is on-site.
    """
    haystack = " ".join(str(c).lower() for c in candidates if c)
    if not haystack.strip():
        return None
    if any(hint in haystack for hint in _HYBRID_HINTS):
        return False
    if any(hint in haystack for hint in _REMOTE_HINTS):
        return True
    return None


def build_record(
    *,
    company: str,
    title: Any,
    job_url: Any,
    ats_provider: str,
    scraping_method: str,
    location: Any = None,
    date_posted: Any = None,
    apply_url: Any = None,
    employment_type: Any = None,
    remote: bool | None = None,
    description: Any = None,
) -> dict[str, Any] | None:
    """Build the canonical normalized record.

    Returns None when the record lacks the minimum identity fields (title and
    URL), so collectors can pass through partial rows without polluting output.
    """
    clean_title = clean_text(title)
    clean_job_url = normalize_url(job_url)
    if not clean_title or not clean_job_url:
        return None

    clean_location = clean_text(location)
    parsed_date = parse_date(date_posted)

    if remote is None:
        remote = infer_remote(clean_title, clean_location)

    return {
        "company": clean_text(company) or company,
        "title": clean_title,
        "location": clean_location,
        "date_posted": format_date(parsed_date),
        "job_url": clean_job_url,
        "apply_url": normalize_url(apply_url),
        "employment_type": clean_text(employment_type),
        "remote": remote,
        "description": strip_html(description) if description else None,
        "ats_provider": ats_provider,
        "scraping_method": scraping_method,
    }


def dedupe_records(records: Iterable[dict]) -> list[dict]:
    """Drop exact repeats of the same normalized job_url within one company."""
    seen: set[str] = set()
    unique: list[dict] = []
    for record in records:
        key = record.get("job_url") or ""
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique
