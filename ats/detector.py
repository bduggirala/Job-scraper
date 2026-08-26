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

import html
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
RADANCY = "radancy"
AMAZON = "amazon"
JOBVITE = "jobvite"
CORNERSTONE = "cornerstone"
JIBE = "jibe"

SUPPORTED_PROVIDERS = (
    WORKDAY, GREENHOUSE, LEVER, ASHBY, SMARTRECRUITERS, PAYLOCITY, UKG,
    TALEO, ICIMS, PHENOM, SUCCESSFACTORS, AVATURE, EIGHTFOLD, RADANCY,
    AMAZON, JOBVITE, CORNERSTONE, JIBE,
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
    # Amazon's own careers site, backed by a public search.json endpoint.
    ("amazon.jobs", AMAZON),
    # Jobvite shares one host across all tenants; the tenant is the first path
    # segment (jobs.jobvite.com/{tenant}/), read in detect_ats below.
    ("jobs.jobvite.com", JOBVITE),
    ("jobvite.com", JOBVITE),
    # Cornerstone OnDemand careersites live at {tenant}.csod.com.
    ("csod.com", CORNERSTONE),
    # Jibe (iCIMS-owned) branded careersites at {tenant}.jibeapply.com.
    ("jibeapply.com", JIBE),
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
    # Self-hosted Avature portals (apply.deloitte.com) never mention avature.net
    # in their HTML - they run on the company's own domain - but their SPA
    # bootstraps a global "avature.portal" config object. Driving the standard
    # Avature /careers/SearchJobs/ endpoint from the branded host returns real
    # jobs (confirmed against Deloitte: 100+ jobs).
    ("avature.portal", AVATURE),
    # Radancy TalentBrew (formerly TMP Worldwide) powers many enterprise
    # careers sites on the company's *own* domain (careers.7-eleven.com), so
    # there is no vendor HOST_PATTERN to match - only these HTML fingerprints.
    # Its assets load from tbcdn.talentbrew.com and its job list is served from
    # a /search-jobs/results endpoint whose module names are highly specific.
    ("talentbrew.com", RADANCY),
    ("data-search-filters-module-name", RADANCY),
    ("data-search-results-module-name", RADANCY),
    ("radancy.net", RADANCY),
    ("amazon.jobs", AMAZON),
    # Jobvite boards embedded in a company's own careers site (e.g. Tyler
    # Technologies) reference jobs.jobvite.com and the jv-job-list markup.
    ("jobs.jobvite.com", JOBVITE),
    ("jv-job-list", JOBVITE),
    # Cornerstone careersites bootstrap an anonymous JWT into csod.context.
    (".csod.com", CORNERSTONE),
    ("csod.context", CORNERSTONE),
    ("jibeapply.com", JIBE),
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

    elif provider == JOBVITE:
        # Every Jobvite tenant shares jobs.jobvite.com; the tenant slug is the
        # first path segment (jobs.jobvite.com/{tenant}/), not a subdomain.
        token = _first_meaningful_segment(segments, skip={"careers", "jobs"})
        result["tenant"] = token
        result["identifier"] = token

    elif provider == AMAZON:
        # Amazon's collector always targets www.amazon.jobs/search.json, so the
        # tenant is nominal - recorded for telemetry only.
        result["tenant"] = "amazon"
        result["identifier"] = "amazon"

    elif provider in (PHENOM, SUCCESSFACTORS, AVATURE, CORNERSTONE, JIBE):
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


#: Path fragments the "any URL" fallback deliberately tolerates. None of these
#: point at a job list, but each is served from the customer's *own* tenant
#: host, so the URL still carries the coordinates a collector needs:
#:
#: * careers.frostbank.com names its Workday tenant only via /external/login;
#: * jobs.nokia.com names its Oracle tenant only via a /siteFavicon/ PNG.
#:
#: Both are confirmed live, and both return real jobs when the API is driven
#: from the host they reveal.
_HOST_BEARING_FRAGMENTS = ("/login", "/signin", "/sign-in", "/logout", "/favicon")


def _is_plausible_job_url(url: str) -> bool:
    """Reject asset, legal and login URLs that merely share the ATS domain."""
    lowered = url.lower()
    path = lowered.split("?", 1)[0]

    if path.endswith(_ASSET_EXTENSIONS):
        return False
    if any(fragment in lowered for fragment in _NON_JOB_PATH_FRAGMENTS):
        return False
    return True


#: Hostname labels that mark a *shared* vendor host - a CDN or asset origin
#: serving every tenant - rather than one customer's own instance. This, not
#: the file extension, is what separates a usable reference from a useless one:
#: Nokia's Oracle tenant is discovered from a favicon PNG on its own host
#: (fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com), while HCLTech's stored ATS URL
#: was a PNG on rmkcdn.successfactors.com - a CDN shared across customers,
#: carrying no tenant coordinates at all.
_SHARED_HOST_MARKERS = ("cdn", "static", "assets", "asset", "media", "img",
                        "images", "content", "resources", "public")


def _is_shared_vendor_host(url: str) -> bool:
    """True when the host looks like a vendor CDN rather than a tenant instance."""
    try:
        host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    except ValueError:
        return False
    labels = host.split(".")
    if not labels:
        return False
    # Only the leading label is judged: "cdn.successfactors.com" and
    # "rmkcdn.successfactors.com" are shared, "career55.sapsf.eu" is not.
    return any(marker in labels[0] for marker in _SHARED_HOST_MARKERS)


def _carries_tenant_coordinates(url: str) -> bool:
    """Looser test for the fallback: not a job board, but still usable.

    A login link, profile page or favicon does not point at a job list, but its
    *host* carries the tenant/site a collector needs - which is the entire
    reason this fallback exists (careers.frostbank.com names its Workday tenant
    only via /external/login; jobs.nokia.com names its Oracle tenant only via a
    favicon, and driving the API from it returns 575 jobs).

    So the rule is about the host, not the file type. Rejected are:

    * shared vendor CDN hosts, which carry no tenant identity;
    * script/style assets, which are served from a shared host even when the
      hostname does not say so (HCLTech's only path-bearing SuccessFactors
      reference is jquery.js on hcm55.sapsf.eu, while its real tenant search
      lives on the sibling career55.sapsf.eu - driving the script host 404s);
    * legal, privacy and support pages, which are the vendor's own site rather
      than the customer's (iCIMS's privacy notice became Builders
      FirstSource's stored ATS URL).
    """
    lowered = url.lower()
    path = lowered.split("?", 1)[0]

    if _is_shared_vendor_host(url):
        return False
    if path.endswith((".js", ".css", ".map")):
        return False
    for fragment in _NON_JOB_PATH_FRAGMENTS:
        if fragment in _HOST_BEARING_FRAGMENTS:
            continue  # the case this fallback exists to serve
        if fragment in lowered:
            return False
    return True


# Hostnames that must never be fetched. Embedded URLs are harvested from
# third-party pages, driven through a collector, then written into the workbook
# and re-fetched on every later run - so an attacker-influenced host is
# persistent, not transient.
_PRIVATE_HOST_RE = re.compile(
    r"^("
    r"localhost|"
    r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"0\.0\.0\.0|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"169\.254\.\d{1,3}\.\d{1,3}|"      # link-local, incl. cloud metadata
    r"\[?::1\]?|"
    r"\[?[fF][cCdD][0-9a-fA-F]{2}:.*\]?"  # IPv6 unique-local
    r")$",
    re.I,
)


def is_safe_fetch_target(url: str) -> bool:
    """True when ``url`` is an ordinary public http(s) URL worth fetching.

    Refuses non-http schemes and private/loopback/link-local hosts. This is a
    hygiene guard on URLs the pipeline did not author, not a complete SSRF
    defence - it does not resolve DNS, so a public name pointing at a private
    address still passes.
    """
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False

    if parts.scheme.lower() not in {"http", "https"}:
        return False

    host = (parts.netloc or "").split("@")[-1]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not host:
        return False

    return not _PRIVATE_HOST_RE.match(host.strip("[]").lower())


def _clean_extracted_url(raw: str) -> str:
    """Tidy a URL pulled straight out of page HTML.

    Entities must be unescaped: a URL scraped from markup arrives as
    ``...External_Careers&amp;foo=1``, and feeding that to the collector
    produces a 404 (observed on Genpact and PepsiCo, both of which failed
    with a literal ``&amp;`` in the requested URL).
    """
    cleaned = html.unescape(raw).strip()
    # Trailing punctuation from the surrounding markup.
    return cleaned.rstrip("\\\"'),;>")


_EMBEDDED_URL_PATTERNS = {
    WORKDAY: r"https?://[\w.-]*\.wd\d+\.myworkday(?:jobs|site)\.com/[^\s\"'<>\\]+",
    GREENHOUSE: r"https?://(?:boards|job-boards)\.greenhouse\.io/[\w-]+",
    LEVER: r"https?://jobs\.lever\.co/[\w-]+",
    ASHBY: r"https?://jobs\.ashbyhq\.com/[\w-]+",
    SMARTRECRUITERS: r"https?://(?:careers|jobs)\.smartrecruiters\.com/[\w-]+",
    ICIMS: r"https?://[\w.-]*\.icims\.com/[^\s\"'<>\\]*",
    # Branded Oracle Cloud Recruiting sites (jobs.nokia.com/en/sites/CX_1/jobs)
    # never mention taleo.net; they embed their API host instead. Extracting
    # that host is what lets TaleoCollector's Oracle Cloud path drive them -
    # confirmed against Nokia, which returns 575 jobs this way.
    TALEO: r"https?://[\w.-]*\.(?:taleo\.net|oraclecloud\.com)(?::\d+)?/[^\s\"'<>\\]*",
    AVATURE: r"https?://[\w.-]*\.avature\.net/[^\s\"'<>\\]*",
    UKG: r"https?://(?:recruiting\.ultipro\.com|[\w.-]+\.ukg\.net)/[^\s\"'<>\\]*",
    PAYLOCITY: r"https?://recruiting\.paylocity\.com/[^\s\"'<>\\]*",
    SUCCESSFACTORS: r"https?://[\w.-]*\.(?:successfactors|sapsf)\.(?:com|eu)/[^\s\"'<>\\]*",
    EIGHTFOLD: r"https?://[\w.-]*\.eightfold\.ai/[^\s\"'<>\\]*",
    # Branded pages that embed a Jobvite board (jobs.jobvite.com/{tenant}) or a
    # Jibe / Cornerstone careersite let the resolver recover the real tenant.
    JOBVITE: r"https?://jobs\.jobvite\.com/[\w-]+",
    JIBE: r"https?://[\w.-]*\.jibeapply\.com/[^\s\"'<>\\]*",
    CORNERSTONE: r"https?://[\w.-]*\.csod\.com/[^\s\"'<>\\]*",
}


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

    patterns = _EMBEDDED_URL_PATTERNS

    pattern = patterns.get(provider)
    if not pattern:
        return None

    for match in re.finditer(pattern, html_text, re.I):
        candidate = _clean_extracted_url(match.group(0))
        if candidate and _is_plausible_job_url(candidate) and is_safe_fetch_target(candidate):
            return candidate
    return None


def extract_any_embedded_ats_url(html_text: str, provider: str) -> str | None:
    """First reference to ``provider``'s domain, job board or not.

    A last resort for branded pages that name their ATS only through a login
    or profile link - confirmed against careers.frostbank.com, whose static
    HTML references ``frostbank.wd5.myworkdayjobs.com`` exclusively via
    ``/external/login``. :func:`extract_embedded_ats_url` rightly rejects
    that as a job board, but the host still carries the tenant and site the
    collector needs, and driving the API from them returns the real jobs.

    Prefers a plausible job URL when one exists, so callers get the better
    signal whenever it is available. Still rejects bare script/style assets
    (.js, .css): those are near-universally served from a shared CDN rather
    than the tenant's own instance, unlike a favicon or login link, which are
    usually hosted alongside the real tenant site (the Oracle Cloud case
    below relies on exactly that for its favicon). Confirmed against
    HCLTech, whose page's only path-bearing SuccessFactors reference is
    ``hcm55.sapsf.eu/.../jquery.js`` (a script host shared across tenants),
    while the real tenant search lives on the sibling ``career55.sapsf.eu``
    host. Driving the API from the script host 404s every time.
    """
    if not html_text:
        return None

    plausible = extract_embedded_ats_url(html_text, provider)
    if plausible:
        return plausible

    pattern = _EMBEDDED_URL_PATTERNS.get(provider)
    if not pattern:
        return None

    for match in re.finditer(pattern, html_text, re.I):
        candidate = _clean_extracted_url(match.group(0))
        if candidate and _carries_tenant_coordinates(candidate) and is_safe_fetch_target(candidate):
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

    patterns = _EMBEDDED_URL_PATTERNS

    pattern = patterns.get(provider)
    if not pattern:
        return []

    seen: list[str] = []
    for match in re.finditer(pattern, html_text, re.I):
        candidate = _clean_extracted_url(match.group(0))
        if (candidate and _is_plausible_job_url(candidate)
                and is_safe_fetch_target(candidate) and candidate not in seen):
            seen.append(candidate)
    return seen
