"""ATS provider detection from a URL.

Given any careers URL, :func:`detect_ats` returns the canonical descriptor::

    {
        "provider": "workday",
        "url": "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One/",
        "identifier": "Capital_One",
        "host": "capitalone.wd12.myworkdayjobs.com",
        "tenant": "capitalone",
        "site": "Capital_One",
    }

``provider`` is :data:`UNKNOWN` when no supported ATS is recognised, which
tells the router to try page-level resolution and then Playwright.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

UNKNOWN = "unknown"

# Canonical provider names used across the whole pipeline.
WORKDAY = "workday"
GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
SMARTRECRUITERS = "smartrecruiters"
PAYLOCITY = "paylocity"
UKG = "ukg"
TALEO = "taleo"
ICIMS = "icims"
PHENOM = "phenom"
SUCCESSFACTORS = "successfactors"
AVATURE = "avature"
EIGHTFOLD = "eightfold"

SUPPORTED_PROVIDERS = (
    WORKDAY, GREENHOUSE, LEVER, ASHBY, SMARTRECRUITERS, PAYLOCITY, UKG,
    TALEO, ICIMS, PHENOM, SUCCESSFACTORS, AVATURE, EIGHTFOLD,
)

# Host-substring -> provider. Checked in order; first match wins.
HOST_PATTERNS: tuple[tuple[str, str], ...] = (
    ("myworkdayjobs.com", WORKDAY),
    ("myworkdaysite.com", WORKDAY),
    ("wd1.myworkdayjobs", WORKDAY),
    ("workday.com", WORKDAY),
    ("greenhouse.io", GREENHOUSE),
    ("boards.greenhouse", GREENHOUSE),
    ("job-boards.greenhouse", GREENHOUSE),
    ("lever.co", LEVER),
    ("ashbyhq.com", ASHBY),
    ("smartrecruiters.com", SMARTRECRUITERS),
    ("recruiting.paylocity.com", PAYLOCITY),
    ("paylocity.com", PAYLOCITY),
    ("ultipro.com", UKG),
    ("ukg.com", UKG),
    ("ukgpro.com", UKG),
    # UKG Pro Recruiting's newer per-tenant domain, e.g.
    # gamestop.rec.pro.ukg.net/{TENANT}/JobBoard/{guid} - same URL shape and
    # same API as recruiting.ultipro.com, just a different host.
    ("ukg.net", UKG),
    ("taleo.net", TALEO),
    ("oraclecloud.com", TALEO),
    ("taleo.com", TALEO),
    ("icims.com", ICIMS),
    ("phenompeople.com", PHENOM),
    ("phenom.com", PHENOM),
    ("successfactors.com", SUCCESSFACTORS),
    ("successfactors.eu", SUCCESSFACTORS),
    ("sapsf.com", SUCCESSFACTORS),
    ("sapsf.eu", SUCCESSFACTORS),
    ("avature.net", AVATURE),
    ("eightfold.ai", EIGHTFOLD),
)

# HTML/script fingerprints used when the host alone is inconclusive.
# Ordered most-specific first.
BODY_FINGERPRINTS: tuple[tuple[str, str], ...] = (
    ("myworkdayjobs.com", WORKDAY),
    ("/wday/cxs/", WORKDAY),
    ("boards.greenhouse.io", GREENHOUSE),
    ("job-boards.greenhouse.io", GREENHOUSE),
    ("greenhouse.io/embed", GREENHOUSE),
    ("api.lever.co", LEVER),
    ("jobs.lever.co", LEVER),
    ("jobs.ashbyhq.com", ASHBY),
    ("api.ashbyhq.com", ASHBY),
    ("api.smartrecruiters.com", SMARTRECRUITERS),
    ("jobs.smartrecruiters.com", SMARTRECRUITERS),
    ("recruiting.paylocity.com", PAYLOCITY),
    ("recruiting.ultipro.com", UKG),
    ("taleo.net", TALEO),
    ("oraclecloud.com/hcmui", TALEO),
    ("/hcmui/candidateexperience", TALEO),
    (".icims.com", ICIMS),
    ("icims_content_iframe", ICIMS),
    ("app.eightfold.ai", EIGHTFOLD),
    ("/api/apply/v2/jobs", EIGHTFOLD),
    ("phenompeople.com", PHENOM),
    ("ph-widget", PHENOM),
    ("ddokey=refinesearch", PHENOM),
    ("phenomcdn", PHENOM),
    # Phenom's client bootstrap object and DOM markers - present on branded
    # sites that never mention the phenompeople.com domain in their HTML.
    ("phapp.ddo", PHENOM),
    ("phapp=", PHENOM),
    ('data-ph-at-id', PHENOM),
    ("ph-page", PHENOM),
    ("successfactors.com", SUCCESSFACTORS),
    ("sapsf.com", SUCCESSFACTORS),
    ("avature.net", AVATURE),
)

_WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[^.]+)\.(?P<pod>wd\d+)\.(?P<domain>myworkdayjobs\.com|myworkdaysite\.com)$", re.I)


def _blank(value: Any) -> bool:
    """True for None, NaN, empty string and common placeholder text."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none", "n/a", "na", "-", "null"}


def _empty_result(url: str | None = None, provider: str = UNKNOWN) -> dict[str, Any]:
    return {
        "provider": provider,
        "url": url,
        "identifier": None,
        "host": None,
        "tenant": None,
        "site": None,
    }


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _parse_workday(url: str, host: str, segments: list[str]) -> dict[str, Any]:
    """Extract Workday tenant + site.

    Handles ``{tenant}.wd{N}.myworkdayjobs.com/{lang}/{site}`` and the
    ``/wday/cxs/{tenant}/{site}`` internal form.
    """
    tenant = None
    match = _WORKDAY_HOST_RE.match(host)
    if match:
        tenant = match.group("tenant")

    site = None
    if "cxs" in segments:
        index = segments.index("cxs")
        remainder = segments[index + 1:]
        if len(remainder) >= 2:
            tenant, site = remainder[0], remainder[1]
    else:
        # Skip a leading locale segment such as "en-US".
        candidates = [s for s in segments if not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", s)]
        if candidates:
            site = candidates[0]

    return {
        "provider": WORKDAY,
        "url": url,
        "identifier": site,
        "host": host,
        "tenant": tenant,
        "site": site,
    }


def _first_meaningful_segment(segments: list[str], skip: set[str] | None = None) -> str | None:
    skip = skip or set()
    for segment in segments:
        lowered = segment.lower()
        if lowered in skip:
            continue
        if re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", lowered):  # locale
            continue
        return segment
    return None


def detect_ats(url: Any) -> dict[str, Any]:
    """Identify the ATS provider behind ``url``.

    Detection is purely lexical - no network calls. Unresolvable URLs come
    back with ``provider == "unknown"`` for the router to resolve or hand to
    Playwright.
    """
    if _blank(url):
        return _empty_result(None)

    raw = str(url).strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")

    try:
        parts = urlsplit(raw)
    except ValueError:
        return _empty_result(str(url))

    host = (parts.netloc or "").lower().split("@")[-1]
    host = host.split(":")[0]
    if not host:
        return _empty_result(raw)

    segments = _path_segments(parts.path)
    provider = UNKNOWN
    for needle, candidate in HOST_PATTERNS:
        if needle in host:
            provider = candidate
            break

    if provider == UNKNOWN:
        return _empty_result(raw)

    if provider == WORKDAY:
        return _parse_workday(raw, host, segments)

    subdomain = host.split(".")[0]
    result = _empty_result(raw, provider)
    result["host"] = host

    if provider == GREENHOUSE:
        # boards.greenhouse.io/{token} | job-boards.greenhouse.io/{token}
        token = _first_meaningful_segment(segments, skip={"embed", "jobs", "boards"})
        if token is None and subdomain not in {"boards", "job-boards", "www", "api", "boards-api"}:
            token = subdomain
        result["identifier"] = token
        result["tenant"] = token

    elif provider == LEVER:
        token = _first_meaningful_segment(segments)
        result["identifier"] = token
        result["tenant"] = token

    elif provider == ASHBY:
        token = _first_meaningful_segment(segments)
        result["identifier"] = token
        result["tenant"] = token

    elif provider == SMARTRECRUITERS:
        token = _first_meaningful_segment(segments, skip={"careers", "jobs", "company"})
        if token is None and subdomain not in {"careers", "jobs", "www", "api"}:
            token = subdomain
        result["identifier"] = token
        result["tenant"] = token

    elif provider == PAYLOCITY:
        # recruiting.paylocity.com/recruiting/jobs/All/{guid}/{slug}
        guid = next(
            (s for s in segments if re.fullmatch(r"[0-9a-f-]{32,36}", s, re.I)), None
        )
        result["identifier"] = guid
        result["tenant"] = guid
        result["site"] = segments[-1] if segments else None

    elif provider == UKG:
        # recruiting.ultipro.com/{TENANTCODE}/JobBoard/{boardGuid}/
        tenant = segments[0] if segments else None
        board = next(
            (s for s in segments if re.fullmatch(r"[0-9a-f-]{32,36}", s, re.I)), None
        )
        result["tenant"] = tenant
        result["site"] = board
        result["identifier"] = board or tenant

    elif provider == TALEO:
        tenant = subdomain
        result["tenant"] = tenant
        result["identifier"] = tenant
        site = _first_meaningful_segment(segments, skip={"careersection", "careersections"})
        result["site"] = site

    elif provider == ICIMS:
        # careers-{company}.icims.com or {company}.icims.com
        tenant = re.sub(r"^careers-", "", subdomain)
        result["tenant"] = tenant
        result["identifier"] = tenant

    elif provider == EIGHTFOLD:
        # Eightfold is often reached through app.eightfold.ai/<endpoint>?domain=
        # {company}.com rather than a company-specific host - the query
        # param, when present, is the authoritative identity, not the
        # generic "app" subdomain.
        query_domain = next(
            (v for k, v in parse_qsl(parts.query) if k.lower() == "domain" and v), None
        )
        tenant = query_domain or subdomain
        result["tenant"] = tenant
        result["identifier"] = tenant
        result["site"] = _first_meaningful_segment(segments)

    elif provider in (PHENOM, SUCCESSFACTORS, AVATURE):
        result["tenant"] = subdomain
        result["identifier"] = subdomain
        result["site"] = _first_meaningful_segment(segments)

    return result


def detect_from_html(html_text: str, final_url: str | None = None) -> str:
    """Detect a provider from page HTML (and the post-redirect URL).

    Used by the resolver for branded career sites that embed or redirect to an
    ATS. Returns a provider name or :data:`UNKNOWN`.
    """
    if final_url:
        detected = detect_ats(final_url)
        if detected["provider"] != UNKNOWN:
            return detected["provider"]

    if not html_text:
        return UNKNOWN

    lowered = html_text.lower()
    for needle, provider in BODY_FINGERPRINTS:
        if needle in lowered:
            return provider
    return UNKNOWN


# Extensions and path fragments that mean a matched URL is an asset, a legal
# page or a login screen rather than a job board. Without these guards the
# first "successfactors.com" hit on a page is often .../extlib/jquery.js, and
# the first "icims.com" hit is often .../legal/privacy-notice-website/.
_ASSET_EXTENSIONS = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff",
    ".woff2", ".ttf", ".eot", ".map", ".mp4", ".webp", ".pdf",
)

_NON_JOB_PATH_FRAGMENTS = (
    "/legal/", "/privacy", "/terms", "/cookie", "/extlib/", "/static/",
    "/assets/", "/scripts/", "/styles/", "/login", "/signin", "/sign-in",
    "/logout", "/support", "/contact", "/accessibility", "/sitemap",
    "/favicon", "/analytics", "/gtm", "/policy",
)


def _is_plausible_job_url(url: str) -> bool:
    """Reject asset, legal and login URLs that merely share the ATS domain."""
    lowered = url.lower()
    path = lowered.split("?", 1)[0]

    if path.endswith(_ASSET_EXTENSIONS):
        return False
    if any(fragment in lowered for fragment in _NON_JOB_PATH_FRAGMENTS):
        return False
    return True


def extract_embedded_ats_url(html_text: str, provider: str) -> str | None:
    """Pull the concrete ATS URL a branded page embeds for ``provider``.

    Example: a corporate careers page that iframes
    ``https://acme.wd5.myworkdayjobs.com/en-US/External`` returns that URL so
    the Workday collector can drive its API directly.

    Candidates that are assets, legal pages or login screens are skipped - a
    page can reference its ATS domain many times, and only some of those
    references point at an actual job board.
    """
    if not html_text:
        return None

    patterns = {
        WORKDAY: r"https?://[\w.-]*\.wd\d+\.myworkday(?:jobs|site)\.com/[^\s\"'<>\\]+",
        GREENHOUSE: r"https?://(?:boards|job-boards)\.greenhouse\.io/[\w-]+",
        LEVER: r"https?://jobs\.lever\.co/[\w-]+",
        ASHBY: r"https?://jobs\.ashbyhq\.com/[\w-]+",
        SMARTRECRUITERS: r"https?://(?:careers|jobs)\.smartrecruiters\.com/[\w-]+",
        ICIMS: r"https?://[\w.-]*\.icims\.com/[^\s\"'<>\\]*",
        TALEO: r"https?://[\w.-]*\.taleo\.net/[^\s\"'<>\\]*",
        AVATURE: r"https?://[\w.-]*\.avature\.net/[^\s\"'<>\\]*",
        UKG: r"https?://(?:recruiting\.ultipro\.com|[\w.-]+\.ukg\.net)/[^\s\"'<>\\]*",
        PAYLOCITY: r"https?://recruiting\.paylocity\.com/[^\s\"'<>\\]*",
        SUCCESSFACTORS: r"https?://[\w.-]*\.(?:successfactors|sapsf)\.(?:com|eu)/[^\s\"'<>\\]*",
        EIGHTFOLD: r"https?://[\w.-]*\.eightfold\.ai/[^\s\"'<>\\]*",
    }

    pattern = patterns.get(provider)
    if not pattern:
        return None

    for match in re.finditer(pattern, html_text, re.I):
        candidate = match.group(0).rstrip("\\\"'),;")
        if candidate and _is_plausible_job_url(candidate):
            return candidate
    return None


def extract_all_embedded_ats_urls(html_text: str, provider: str) -> list[str]:
    """Every plausible embedded URL for ``provider``, not just the first.

    Large companies often run several regional tenants of the same ATS (e.g.
    a US Workday tenant and a separate MEISA/APAC one). A single search-result
    page can embed links into more than one of them at once, so picking "the
    first match" can silently return the wrong region. Callers should use
    this to check whether all matches resolve to the *same* tenant/site before
    treating any one of them as authoritative.
    """
    if not html_text:
        return []

    patterns = {
        WORKDAY: r"https?://[\w.-]*\.wd\d+\.myworkday(?:jobs|site)\.com/[^\s\"'<>\\]+",
        GREENHOUSE: r"https?://(?:boards|job-boards)\.greenhouse\.io/[\w-]+",
        LEVER: r"https?://jobs\.lever\.co/[\w-]+",
        ASHBY: r"https?://jobs\.ashbyhq\.com/[\w-]+",
        SMARTRECRUITERS: r"https?://(?:careers|jobs)\.smartrecruiters\.com/[\w-]+",
        ICIMS: r"https?://[\w.-]*\.icims\.com/[^\s\"'<>\\]*",
        TALEO: r"https?://[\w.-]*\.taleo\.net/[^\s\"'<>\\]*",
        AVATURE: r"https?://[\w.-]*\.avature\.net/[^\s\"'<>\\]*",
        UKG: r"https?://(?:recruiting\.ultipro\.com|[\w.-]+\.ukg\.net)/[^\s\"'<>\\]*",
        PAYLOCITY: r"https?://recruiting\.paylocity\.com/[^\s\"'<>\\]*",
        SUCCESSFACTORS: r"https?://[\w.-]*\.(?:successfactors|sapsf)\.(?:com|eu)/[^\s\"'<>\\]*",
        EIGHTFOLD: r"https?://[\w.-]*\.eightfold\.ai/[^\s\"'<>\\]*",
    }

    pattern = patterns.get(provider)
    if not pattern:
        return []

    seen: list[str] = []
    for match in re.finditer(pattern, html_text, re.I):
        candidate = match.group(0).rstrip("\\\"'),;")
        if candidate and _is_plausible_job_url(candidate) and candidate not in seen:
            seen.append(candidate)
    return seen
