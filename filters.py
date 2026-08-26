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

# --- workplace / remote scope ---------------------------------------------
# "Remote" is not one thing, and collapsing it to a boolean let the wrong jobs
# through: "Remote - must reside in New York" satisfied both a remote token and
# the US check (any state name counted as US eligibility), so it reached the
# output as a DFW/remote match while being neither.
REMOTE_US = "remote_us"                 # remote, anywhere in the U.S.
REMOTE_RESTRICTED = "remote_restricted"  # remote, but tied to a non-Texas state
REMOTE_NON_US = "remote_non_us"          # remote, outside the U.S.
WORKPLACE_HYBRID = "hybrid"              # part on-site
WORKPLACE_ONSITE = "onsite"              # ordinary office location

_HYBRID_TOKENS = (
    "hybrid", "days onsite", "days on-site", "days in office", "days in-office",
    "flex office", "flexible office",
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


#: The country named directly. "usa"/"u.s.a."/"u.s."/a standalone "us" all
#: count; the earlier pattern required literal dots, so a bare "Remote, USA"
#: was only rescued by the remote-token clause that has now been removed.
_US_COUNTRY_RE = re.compile(
    r"\bunited states\b|\bu\.?s\.?a\.?\b|\busa\b|\bus\b", re.I
)


def _geographic_residue(location: str) -> str:
    """What is left of a location once remote wording is taken out.

    ``"Remote"`` on its own leaves nothing - there is no geography to judge, so
    the posting is trusted. ``"Remote (Pernambuco, Recife)"`` leaves two place
    names, which is a claim about where the role sits and therefore something
    that needs positive U.S. evidence before it counts as U.S.-eligible.
    """
    text = location.lower()
    for token in sorted(_REMOTE_TOKENS, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(token)}\b", " ", text)
    # Punctuation, connectives and role-agnostic filler carry no geography.
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\b(in|the|and|or|of|only|based|position|role|usa?)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _named_states(text: str) -> set[str]:
    """U.S. states named in ``text``, by full name or "City, ST" abbreviation.

    Abbreviations require a preceding comma, matching the convention
    :meth:`LocationMatcher._conflicting_state` already uses. Without that
    anchor two-letter state codes collide with ordinary words - "in" is
    Indiana, so "Remote (Anywhere in the US)" read as a state-restricted role.
    """
    found: set[str] = set()
    for abbrev, name in _STATE_TOKENS.items():
        if re.search(rf"\b{re.escape(name)}\b", text, re.I):
            found.add(abbrev)
        elif re.search(rf",\s*{abbrev}\b", text, re.I):
            found.add(abbrev)
    return found


def classify_remote_scope(record: dict[str, Any]) -> str:
    """Classify a record's workplace arrangement.

    Returns one of :data:`REMOTE_US`, :data:`REMOTE_RESTRICTED`,
    :data:`REMOTE_NON_US`, :data:`WORKPLACE_HYBRID` or
    :data:`WORKPLACE_ONSITE`.

    Hybrid is checked before remote because "Hybrid - Dallas" contains no
    remote token but "Remote/Hybrid" contains both, and part-onsite is the
    stronger constraint. A remote role naming exactly one non-Texas state is
    *restricted*, not U.S.-wide - that distinction is the whole point, since a
    state name used to count as positive evidence of U.S. eligibility.
    """
    location = (record.get("location") or "").lower()
    title = (record.get("title") or "").lower()
    haystack = f"{location} {title}"

    if any(token in haystack for token in _HYBRID_TOKENS):
        return WORKPLACE_HYBRID

    explicit = record.get("remote")
    has_token = any(token in haystack for token in _REMOTE_TOKENS)
    if explicit is not True and not has_token:
        return WORKPLACE_ONSITE

    if any(re.search(rf"\b{re.escape(t)}\b", haystack) for t in _NON_US_TOKENS):
        return REMOTE_NON_US

    states = _named_states(haystack)
    if states and "tx" not in states:
        # Remote but pinned to somewhere else. Several states named together
        # ("Remote - NY, NJ, CT") is still a restriction, just a broader one.
        return REMOTE_RESTRICTED

    # A blank or generic location ("Remote", "Work from home") carries no
    # evidence either way and is trusted. A location that still names a place
    # after the remote wording is removed is making a claim about where the
    # role sits, and that needs positive U.S. evidence - a blocklist of foreign
    # countries cannot be exhaustive, and every city name it misses would
    # otherwise pass as U.S.-eligible.
    if _geographic_residue(location) and not LocationMatcher._has_us_signal(haystack):
        return REMOTE_NON_US
    return REMOTE_US


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

        An exclude pattern anywhere in the *whole* title disqualifies it
        outright - confirmed against real scraped titles like "Senior
        Manager, Data Science - US Card", where "Data Science" alone would
        otherwise match on its own comma segment while "Senior Manager"
        sits in a different one. A per-segment-only exclude check would
        accept that as an individual-contributor role; it is not.
        """
        if not title:
            return False
        if any(pattern.search(title) for pattern in self.exclude):
            return False
        for segment in self.segments(title):
            if any(pattern.search(segment) for pattern in self.include):
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

    @staticmethod
    def _has_us_signal(text: str) -> bool:
        """True when the text names something recognizably American.

        A curated non-US blocklist (``_NON_US_TOKENS``) can never be
        exhaustive - confirmed live: an Accenture posting listing only
        Brazilian states/cities (Pernambuco, Recife, Maranhão, Porto
        Alegre...) named none of our blocked tokens (only the country name
        "brazil" is blocked, not its city names) and slipped through as
        "remote_us". This is the positive-evidence counterpart: a detailed,
        non-generic location listing is only trusted as U.S.-eligible when it
        actually names a U.S. state or the country.

        This deliberately does **not** accept a remote marker as evidence.
        It used to end with ``any(token in text for token in _REMOTE_TOKENS)``,
        and since the text being tested is a remote job's location, that clause
        matched every single time - so the function returned True for every
        remote posting and the guard above it never ran at all. A bare
        "Remote" with nothing else in it is handled by the caller, which can
        see that there is no other geography to judge.
        """
        if _US_COUNTRY_RE.search(text):
            return True
        for abbrev, name in _STATE_TOKENS.items():
            # re.I: _STATE_TOKENS holds lowercase names and the text reaching
            # here is not always lowercased, so a case-sensitive match silently
            # ignored every state spelled out in full.
            if re.search(rf"\b{re.escape(name)}\b", text, re.I):
                return True
            if re.search(rf",\s*{abbrev}\b", text, re.I):
                return True
            if re.search(rf"\bremote[\s-]*{abbrev}\b", text, re.I):
                return True
        return False

    def is_remote_us(self, record: dict[str, Any]) -> bool:
        """True only for remote roles open anywhere in the U.S.

        Delegates to :func:`classify_remote_scope`, so a role restricted to one
        non-Texas state, a hybrid role, and a non-U.S. remote role are all
        correctly excluded rather than collapsed into "has a remote token and
        mentions somewhere American".
        """
        if not self.include_remote:
            return False
        return classify_remote_scope(record) == REMOTE_US

    def _conflicting_state(self, text: str, matched_city: str) -> bool:
        """True when the string names a U.S. state other than Texas.

        The abbreviation branch below has always been case-insensitive; this
        one was not, and ``_STATE_TOKENS`` holds lowercase names while
        ``text`` is the location exactly as scraped. Every state written out
        in full therefore went unnoticed, so the guard only ever worked on the
        "City, ST" form - confirmed against a real harvest, where eight Amazon
        postings in "Arlington, Virginia, USA" matched as DFW.
        """
        for abbrev, name in _STATE_TOKENS.items():
            if abbrev == "tx":
                continue
            if re.search(rf"\b{re.escape(name)}\b", text, re.I):
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
        # Carried into the output so "remote" is legible rather than a bare
        # flag: remote_us / remote_restricted / remote_non_us / hybrid / onsite.
        enriched["remote_scope"] = classify_remote_scope(record)

        if status == WITHIN_WINDOW:
            counts["within_window"] += 1
        elif status == DATE_UNAVAILABLE:
            counts["date_unavailable"] += 1
        else:
            counts["older_than_window"] += 1
            continue  # only genuinely stale jobs are dropped

        kept.append(enriched)

    return {"jobs": kept, "counts": counts}
