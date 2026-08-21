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
        A list of ``{"title", "job_url", "location"}`` dicts. ``location`` is
        None unless a sibling element clearly carries it.
    """
    soup = make_soup(html_text)
    anchors = soup.select(selector) if selector else soup.find_all("a", href=True)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in anchors:
        href = anchor.get("href")
        if not href:
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
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
            text = clean_text(node.get_text(" ", strip=True))
            if text:
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
