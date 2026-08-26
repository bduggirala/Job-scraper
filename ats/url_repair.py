"""Repair careers URLs whose hostname no longer exists.

Several workbook entries point at ``careers.<company>.com`` subdomains that
have since been retired - confirmed for 9 distinct companies (Sabre, NTT DATA,
Celanese, Concentra, Cotality, Cook Children's, Addus, FM, Primoris): the
subdomain returns NXDOMAIN while the company's root domain resolves fine and
still hosts a careers page.

Rather than recording those as permanent failures, this module derives
candidate hostnames from the dead one, finds a live root, and locates the
careers link on it. Purely additive: a URL whose host already resolves is
returned untouched, and a repair that finds nothing returns None so the
caller keeps the original behaviour.
"""

from __future__ import annotations

import re
import socket
import time
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

import http_client
from ats.html_utils import extract_job_links, make_soup
from logger import get_logger

log = get_logger("ats.url_repair")

# Anchor text / href fragments that mark the careers entry point on a
# corporate homepage.
CAREERS_LINK_PATTERNS = (
    "careers", "career", "jobs", "job-search", "work-with-us",
    "join-us", "join-our-team", "employment", "opportunities",
)

CAREERS_PATH_GUESSES = (
    "/careers", "/careers/", "/en/careers", "/en-us/careers",
    "/about/careers", "/company/careers", "/jobs", "/careers/jobs",
)

# Subdomain labels worth stripping when the full host is dead.
_STRIPPABLE_LABELS = {"careers", "career", "jobs", "job", "apply", "recruiting", "www"}

#: Pause before re-checking a host that failed to resolve. Long enough to ride
#: out a momentary resolver hiccup, short enough not to slow a real run.
_RESOLVE_RETRY_DELAY = 1.0


def _resolves_once(host: str) -> bool:
    """A single DNS lookup. Split out so the retry logic is testable."""
    try:
        socket.getaddrinfo(host, None)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


@lru_cache(maxsize=512)
def host_resolves(host: str) -> bool:
    """True when a hostname resolves in DNS. Cached - repair retries a lot.

    A *failure* is confirmed with a second lookup after a short pause, because
    declaring a host dead is consequential: it triggers repair, and a verified
    repair is written back over the workbook's URL. The README documents
    Chromium's resolver buckling under concurrent browser instances, and the
    same conditions make getaddrinfo fail spuriously here - so one bad lookup
    must not be enough to permanently rewrite a working URL.

    A host that resolves costs exactly one lookup, which is the common case.
    """
    if not host:
        return False
    if _resolves_once(host):
        return True

    time.sleep(_RESOLVE_RETRY_DELAY)
    if _resolves_once(host):
        log.debug("%s failed one DNS lookup then resolved; not treating as dead", host)
        return True
    return False


def _root_domain(host: str) -> str:
    """Best-effort registrable domain (last two labels)."""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    # Handle simple two-part public suffixes (.co.uk, .com.au).
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _candidate_hosts(host: str) -> list[str]:
    """Live hostnames to try in place of a dead one, best first."""
    root = _root_domain(host)
    labels = host.split(".")

    candidates: list[str] = []
    # Drop a leading careers/jobs/www label: careers.acme.com -> acme.com
    if labels and labels[0].lower() in _STRIPPABLE_LABELS:
        candidates.append(".".join(labels[1:]))
    candidates.extend([f"www.{root}", root])

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate != host and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _looks_like_careers_page(html_text: str) -> bool:
    """True when a page actually links to job openings.

    Guards the repair against "successfully" landing on a corporate homepage,
    which returns HTTP 200 and plenty of HTML but no jobs.

    Counting generic word hits was far too weak for that job: "job", "career"
    and "position" appear on almost every corporate page, so three hits was
    close to always true. The page must now carry links that look like
    individual postings, or a search control - structure, not vocabulary.
    """
    if not html_text or len(html_text) < 2000:
        return False

    if extract_job_links(html_text, "https://example.invalid/"):
        return True

    # No individual postings, but a job *search* entry point is still a
    # careers page - the listing lives one interaction away.
    lowered = html_text.lower()
    return any(
        marker in lowered
        for marker in ("search jobs", "search for jobs", "job search",
                       "view all jobs", "current openings", "/job-search")
    )


def _find_careers_link(html_text: str, base_url: str) -> str | None:
    """Locate the careers entry point on a corporate homepage."""
    soup = make_soup(html_text)
    best: tuple[int, str] | None = None

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = (anchor.get_text(" ", strip=True) or "").lower()
        lowered_href = href.lower()

        score = 0
        for pattern in CAREERS_LINK_PATTERNS:
            if text == pattern:
                score = max(score, 100)
            elif pattern in text:
                score = max(score, 70)
            if pattern in lowered_href:
                score = max(score, score + 20)

        if score > 0 and (best is None or score > best[0]):
            best = (score, urljoin(base_url, href))

    return best[1] if best else None


def repair_careers_url(company: str, url: str) -> str | None:
    """Find a working careers URL when ``url``'s hostname is dead.

    Returns a replacement URL, or None when the host is fine (nothing to do)
    or no live alternative could be found.
    """
    try:
        parts = urlsplit(url if re.match(r"^https?://", url, re.I) else f"https://{url}")
    except ValueError:
        return None

    host = (parts.netloc or "").split(":")[0].lower()
    if not host or host_resolves(host):
        return None

    log.debug("%s: host %s does not resolve; attempting repair", company, host)

    original_path = (parts.path or "").rstrip("/")

    for candidate_host in _candidate_hosts(host):
        if not host_resolves(candidate_host):
            continue

        # 1. The original path, but only when it carried real information -
        #    a bare "/" would just land on the homepage, which is not a
        #    careers page and would make the repair look successful while
        #    yielding no jobs.
        paths = ([original_path] if original_path else []) + list(CAREERS_PATH_GUESSES)

        for path in paths:
            candidate = f"https://{candidate_host}{path}"
            try:
                response = http_client.request(candidate, method="GET", allow_redirects=True)
            except Exception:
                continue
            if response.status_code < 400 and _looks_like_careers_page(response.text):
                log.info("%s: repaired dead URL %s -> %s", company, url, response.url)
                return str(response.url)

        # 2. Fall back to scanning the homepage for a careers link.
        try:
            homepage = http_client.request(f"https://{candidate_host}/", method="GET")
        except Exception:
            continue

        careers_url = _find_careers_link(homepage.text, str(homepage.url))
        if careers_url:
            log.info("%s: repaired dead URL %s -> %s (via homepage)", company, url, careers_url)
            return careers_url

    log.debug("%s: could not repair dead URL %s", company, url)
    return None
