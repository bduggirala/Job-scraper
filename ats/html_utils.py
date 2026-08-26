"""HTML parsing helpers for ATS platforms without a clean public JSON API.

Several enterprise ATS products (iCIMS, Avature, SuccessFactors, some Taleo
and Paylocity tenants) only expose server-rendered job lists. These helpers
turn such pages into candidate (title, url, location) triples without each
collector re-implementing soup handling.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from normalize import clean_text

# Anchor text that is navigation, not a job title.
_NAV_NOISE = {
    "apply", "apply now", "view job", "details", "job details", "learn more",
    "read more", "next", "previous", "back", "search", "save job", "share",
    "sign in", "login", "home", "view all jobs", "see all jobs", "more",
}

_JOB_HREF_HINTS = (
    "/job/", "/jobs/", "/careers/jobdetail", "/jobdetail", "/opportunitydetail",
    "/requisition", "/vacancy", "/position", "/openings/", "/posting/",
    "jobid=", "requisitionid=", "opportunityid=", "jobpostingid=",
)


#: Classes that mark an element as present for assistive technology only. Such
#: an element names a field ("Job Posting Title", "Job Locations"); it is never
#: the field's value. Reading it as text prefixed every iCIMS title with its own
#: column header - "Job Posting Title Senior Manager, Service Design".
_SR_ONLY_CLASS_RE = re.compile(
    r"\b(sr-only|sronly|visually-hidden|visuallyhidden|screen-reader-text|"
    r"screenreader|a11y-hidden|field-label|hidden-label|assistive-text)\b", re.I
)

#: A screen-reader label whose text names a location field. Used to find the
#: value sitting next to it, since such markup carries the field name in the
#: label's *text* rather than in any class an element search could match.
_LOCATION_LABEL_RE = re.compile(r"\b(job\s+)?locations?\b|\bcity\b|\bwork\s+site\b", re.I)


def _is_sr_only(node: Any) -> bool:
    classes = node.get("class") if hasattr(node, "get") else None
    if not classes:
        return False
    return bool(_SR_ONLY_CLASS_RE.search(" ".join(classes)))


def visible_text(node: Any) -> str:
    """``node``'s text with screen-reader-only labels left out.

    ``get_text()`` returns everything, including the ``sr-only`` spans that
    label a field for assistive technology. Those are field *names*, so they
    belong in neither a title nor a location.
    """
    if node is None:
        return ""
    parts = [
        text for text in node.find_all(string=True)
        if not any(_is_sr_only(parent) for parent in text.parents)
    ]
    return clean_text(" ".join(parts).strip()) or ""


def make_soup(html_text: str) -> BeautifulSoup:
    """Parse HTML with lxml, degrading to the stdlib parser if unavailable."""
    try:
        return BeautifulSoup(html_text, "lxml")
    except Exception:  # pragma: no cover - lxml missing/broken
        return BeautifulSoup(html_text, "html.parser")


def looks_like_job_link(href: str, text: str | None) -> bool:
    """Heuristic: does this anchor point at an individual job posting?"""
    if not href:
        return False
    lowered = href.lower()
    if not any(hint in lowered for hint in _JOB_HREF_HINTS):
        return False
    cleaned = (text or "").strip().lower()
    if not cleaned or cleaned in _NAV_NOISE or len(cleaned) < 3:
        return False
    return True


def extract_job_links(
    html_text: str,
    base_url: str,
    selector: str | None = None,
) -> list[dict[str, Any]]:
    """Extract candidate job links from a rendered job list page.

    Args:
        html_text: raw page HTML.
        base_url: used to absolutize relative hrefs.
        selector: optional CSS selector to narrow the search.

    Returns:
        A list of ``{"title", "job_url", "location", "date_posted"}`` dicts.
        ``location`` and ``date_posted`` are None unless a sibling element
        clearly carries them.
    """
    soup = make_soup(html_text)
    anchors = soup.select(selector) if selector else soup.find_all("a", href=True)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in anchors:
        href = anchor.get("href")
        if not href:
            continue
        title = visible_text(anchor)
        if not selector and not looks_like_job_link(href, title):
            continue
        if not title:
            continue

        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)

        results.append({
            "title": title,
            "job_url": absolute,
            "location": _nearby_location(anchor),
            "date_posted": _nearby_date(anchor),
        })

    return results


def _nearby_location(anchor: Any) -> str | None:
    """Look for a location string in the anchor's immediate neighbourhood."""
    location_pattern = re.compile(
        r"(location|city|region|jobLocation|job-location)", re.I
    )

    container = anchor.parent
    for _ in range(3):
        if container is None:
            break
        node = container.find(
            attrs={"class": location_pattern}
        ) or container.find(attrs={"data-ph-at-id": location_pattern})
        if node:
            text = visible_text(node)
            if text:
                return text
        labelled = _location_beside_its_label(container)
        if labelled:
            return labelled
        container = container.parent
    return None


def _location_beside_its_label(container: Any) -> str | None:
    """A location sitting next to a screen-reader label that names it.

    The class-based search above cannot see this markup: the element holding
    "US-TX-Westlake" carries no location-ish class, and the only thing
    identifying it is a sibling ``<span class="sr-only field-label">Job
    Locations</span>``. Every iCIMS tenant renders its job cards this way, and
    a blank location fails the DFW match - so 309 of Charles Schwab's 309
    postings were dropped, all of them in Westlake, Texas.
    """
    for label in container.find_all(_is_sr_only):
        if not _LOCATION_LABEL_RE.search(label.get_text(" ", strip=True)):
            continue
        # The value is whatever the label's own container says once the label
        # itself is removed from consideration.
        text = visible_text(label.parent)
        if text:
            return text
    return None


def _nearby_date(anchor: Any) -> str | None:
    """Look for a posting date in the anchor's immediate neighbourhood.

    Mirrors :func:`_nearby_location`. A ``<time datetime=...>`` attribute is
    preferred because it is machine-formatted; a class-marked element's text
    is the fallback. Returns the raw string - parsing is normalize's job.
    """
    date_pattern = re.compile(r"(date|posted|jobDate|job-date)", re.I)

    container = anchor.parent
    for _ in range(3):
        if container is None:
            break
        time_node = container.find("time")
        if time_node is not None:
            stamp = time_node.get("datetime")
            if stamp:
                return clean_text(stamp)
        node = container.find(attrs={"class": date_pattern})
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if text and len(text) < 60:
                return text
        container = container.parent
    return None


def iter_jsonld_jobs(html_text: str) -> Iterator[dict[str, Any]]:
    """Yield schema.org JobPosting objects embedded as JSON-LD.

    Many branded career sites emit JSON-LD for SEO even when their job list is
    client-rendered, which gives us reliable titles, dates and locations
    without a browser.
    """
    soup = make_soup(html_text)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue

        for node in _walk_jsonld(data):
            if str(node.get("@type", "")).lower() == "jobposting":
                yield node


def _walk_jsonld(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_jsonld(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def jsonld_location(node: dict[str, Any]) -> str | None:
    """Flatten a schema.org jobLocation into a display string."""
    location = node.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return clean_text(location) if location else None

    address = location.get("address")
    if isinstance(address, list):
        address = address[0] if address else None
    if not isinstance(address, dict):
        return clean_text(address) if address else None

    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry") if isinstance(address.get("addressCountry"), str) else None,
    ]
    joined = ", ".join(clean_text(p) for p in parts if clean_text(p))
    return joined or None
