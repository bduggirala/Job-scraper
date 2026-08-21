"""Filtering: target roles, DFW/remote locations, and the freshness window.

All three filters are pure functions over normalized records so they can be
tested and reordered independently.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from normalize import parse_date
from settings import Settings, load_settings

# Date filter status values. Deliberately window-agnostic: the actual cutoff
# comes from settings.yaml's hours_old, so baking "72" into the label would
# misstate the data whenever that value changes.
WITHIN_WINDOW = "within_window"
OUTSIDE_WINDOW = "older_than_window"
DATE_UNAVAILABLE = "date_unavailable"

# Title segments are split on these so "Software Engineer, Data Engineering"
# is judged on the segment that actually names the role.
_SEGMENT_SPLIT = re.compile(r"[,;|/\\–—]|\s+-\s+|\(|\)|\[|\]")

# U.S. state names/abbreviations used to reject same-named cities elsewhere
# (Westlake Village CA, Richardson UT, Frisco CO...).
_STATE_TOKENS = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming",
}

_REMOTE_TOKENS = (
    "remote", "work from home", "work-from-home", "wfh", "telecommute",
    "virtual", "anywhere in the us", "anywhere", "home based", "home-based",
    "us-remote", "remote - us", "nationwide", "distributed",
)

# Remote roles outside the U.S. must not qualify.
_NON_US_TOKENS = (
    "india", "canada", "mexico", "united kingdom", "uk", "ireland", "germany",
    "france", "spain", "poland", "romania", "netherlands", "singapore",
    "australia", "japan", "china", "brazil", "argentina", "philippines",
    "emea", "apac", "latam", "europe", "asia", "bengaluru", "bangalore",
    "hyderabad", "pune", "chennai", "mumbai", "noida", "gurgaon", "toronto",
    "vancouver", "london", "dublin", "berlin", "paris", "madrid", "warsaw",
)


class RoleMatcher:
    """Case-insensitive target-role matcher with false-positive guards."""

    def __init__(self, settings: Settings | None = None):
        cfg = settings or load_settings()
        self.include = [
            re.compile(pattern, re.I) for pattern in cfg.get("target_role_patterns", [])
        ]
        self.exclude = [
            re.compile(pattern, re.I) for pattern in cfg.get("exclude_role_patterns", [])
        ]

    @staticmethod
    def segments(title: str) -> list[str]:
        parts = [p.strip() for p in _SEGMENT_SPLIT.split(title) if p and p.strip()]
        # Always test the whole title too, so multi-word roles that span a
        # separator ("Engineer II - Data") still get a chance to match.
        return parts + [title.strip()] if parts else [title.strip()]

    def matches(self, title: str | None) -> bool:
        """True when any title segment names a target role.

        A segment matching an exclude pattern is rejected even if it also
        matched an include pattern, which is what keeps "Data Scientist" and
        "Machine Learning Engineer" out.
        """
        if not title:
            return False
        for segment in self.segments(title):
            if not any(pattern.search(segment) for pattern in self.include):
                continue
            if any(pattern.search(segment) for pattern in self.exclude):
                continue
            return True
        return False


class LocationMatcher:
    """DFW-metro and remote-US location matcher."""

    def __init__(self, settings: Settings | None = None):
        cfg = settings or load_settings()
        self.cities = [str(c).strip().lower() for c in cfg.get("locations", []) if str(c).strip()]
        self.include_remote = bool(cfg.get("include_remote", True))
        self.enforce_texas = bool(cfg.get("enforce_texas_for_city_match", True))
        self._city_patterns = [
            (city, re.compile(rf"\b{re.escape(city)}\b", re.I)) for city in self.cities
        ]

    @staticmethod
    def _has_non_us_signal(text: str) -> bool:
        return any(re.search(rf"\b{re.escape(token)}\b", text) for token in _NON_US_TOKENS)

    def is_remote_us(self, record: dict[str, Any]) -> bool:
        """True when the record is a U.S.-eligible remote role."""
        if not self.include_remote:
            return False

        location = (record.get("location") or "").lower()
        title = (record.get("title") or "").lower()
        haystack = f"{location} {title}"

        explicit_remote = record.get("remote")
        has_token = any(token in haystack for token in _REMOTE_TOKENS)

        if explicit_remote is not True and not has_token:
            return False
        if self._has_non_us_signal(haystack):
            return False
        return True

    def _conflicting_state(self, text: str, matched_city: str) -> bool:
        """True when the string names a U.S. state other than Texas."""
        for abbrev, name in _STATE_TOKENS.items():
            if abbrev == "tx":
                continue
            if re.search(rf"\b{re.escape(name)}\b", text):
                return True
            # Abbreviations only count in a "City, ST" shape to avoid matching
            # random two-letter words.
            if re.search(rf",\s*{abbrev.upper()}\b", text, re.I):
                return True
        return False

    def is_dfw(self, record: dict[str, Any]) -> bool:
        """True when the location names a DFW-metro city."""
        location = record.get("location")
        if not location:
            return False
        text = str(location)

        for city, pattern in self._city_patterns:
            if not pattern.search(text):
                continue
            if self.enforce_texas and self._conflicting_state(text, city):
                continue
            return True
        return False

    def matches(self, record: dict[str, Any]) -> tuple[bool, str | None]:
        """Return (matched, reason) where reason is 'dfw' or 'remote_us'."""
        if self.is_dfw(record):
            return True, "dfw"
        if self.is_remote_us(record):
            return True, "remote_us"
        return False, None


def classify_date(
    record: dict[str, Any],
    hours_old: int = 72,
    *,
    now: datetime | None = None,
    first_seen: str | datetime | None = None,
) -> str:
    """Classify a record against the freshness window.

    Falls back to ``first_seen`` from the SQLite tracker when the ATS gives no
    posting date - a job first observed inside the window is treated as newly
    discovered rather than being discarded.
    """
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(hours=hours_old)

    posted = parse_date(record.get("date_posted"), reference=reference)
    if posted is not None:
        return WITHIN_WINDOW if posted >= cutoff else OUTSIDE_WINDOW

    seen = parse_date(first_seen, reference=reference) if first_seen else None
    if seen is not None:
        return WITHIN_WINDOW if seen >= cutoff else OUTSIDE_WINDOW

    return DATE_UNAVAILABLE


def apply_filters(
    records: Iterable[dict[str, Any]],
    settings: Settings | None = None,
    *,
    first_seen_lookup: dict[str, str] | None = None,
    now: datetime | None = None,
    enricher: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run the full filter chain and report counts at each stage.

    Order matters: the cheap role filter runs first, the optional ``enricher``
    runs on the survivors only (it makes network calls), and the location and
    date filters then judge the enriched data.

    Returns:
        ``{"jobs": [...], "counts": {...}}`` where ``jobs`` are records that
        passed role + location filters and were not classified as
        ``older_than_window``. Records with an unknown date are kept and
        flagged via ``date_filter_status``.
    """
    cfg = settings or load_settings()
    hours_old = int(cfg.get("hours_old", 72))
    role_matcher = RoleMatcher(cfg)
    location_matcher = LocationMatcher(cfg)
    lookup = first_seen_lookup or {}

    all_records = list(records)
    counts = {
        "collected": len(all_records),
        "target_role": 0,
        "location_match": 0,
        "within_window": 0,
        "date_unavailable": 0,
        "older_than_window": 0,
    }

    role_matched = [r for r in all_records if role_matcher.matches(r.get("title"))]
    counts["target_role"] = len(role_matched)

    if enricher and role_matched:
        role_matched = enricher(role_matched)

    kept: list[dict[str, Any]] = []

    for record in role_matched:
        matched, reason = location_matcher.matches(record)
        if not matched:
            continue
        counts["location_match"] += 1

        status = classify_date(
            record, hours_old, now=now, first_seen=lookup.get(record.get("job_id", ""))
        )
        enriched = dict(record)
        enriched["date_filter_status"] = status
        enriched["location_match_type"] = reason

        if status == WITHIN_WINDOW:
            counts["within_window"] += 1
        elif status == DATE_UNAVAILABLE:
            counts["date_unavailable"] += 1
        else:
            counts["older_than_window"] += 1
            continue  # only genuinely stale jobs are dropped

        kept.append(enriched)

    return {"jobs": kept, "counts": counts}
