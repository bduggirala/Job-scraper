"""Generic schema.org ``JobPosting`` (JSON-LD) fallback collector.

Many career pages - especially individual job-detail pages, but also some list
pages - embed ``<script type="application/ld+json">`` blocks carrying
schema.org ``JobPosting`` structured data for SEO. Reading that gives us
reliable titles, locations, dates and URLs for a long tail of *unknown*
providers with no per-provider code.

This collector is deliberately provider-agnostic: it GETs :attr:`self.url`,
parses every JSON-LD block, and extracts every ``JobPosting`` entity it finds,
regardless of which ATS rendered the page. It is meant to sit as a tier between
page resolution and the Playwright browser fallback - if a page exposes
JobPosting JSON-LD we can harvest it cheaply over HTTP; otherwise
:class:`CollectorUnavailable` is raised and the router escalates to Playwright.

The three JSON-LD shapes we must tolerate:

* a single object with ``@type == "JobPosting"``;
* a list of objects (``[{...}, {...}]``), any of which may be a JobPosting;
* an ``@graph`` wrapper (``{"@graph": [...]}``) mixing a JobPosting in among
  other entity types (``Organization``, ``BreadcrumbList``, ...).

Every entity is parsed defensively: a malformed or partial JobPosting is
skipped, never fatal, so one bad block cannot sink a page that also carries a
good one.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import http_client
from ats.base import (
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
    STOP_MORE_AVAILABLE,
)
from ats.html_utils import detect_more_results, make_soup
from normalize import clean_text

#: Canonical provider name for records this collector emits. Kept local to this
#: module (detector.py is intentionally untouched); the router references it via
#: the ``"jsonld"`` key.
JSONLD = "jsonld"


def _iter_jsonld_blocks(html_text: str) -> Iterator[Any]:
    """Yield the parsed JSON value of every ``application/ld+json`` block.

    Blocks that fail to parse are skipped silently - malformed JSON-LD is
    common in the wild and must never be fatal.
    """
    soup = make_soup(html_text)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            yield json.loads(raw)
        except (ValueError, TypeError):
            continue


def _is_job_posting(node: Any) -> bool:
    """True when a JSON-LD node is (or lists) the ``JobPosting`` type."""
    if not isinstance(node, dict):
        return False
    node_type = node.get("@type")
    if isinstance(node_type, str):
        return node_type.lower() == "jobposting"
    if isinstance(node_type, list):
        return any(isinstance(t, str) and t.lower() == "jobposting" for t in node_type)
    return False


def _iter_job_postings(html_text: str) -> Iterator[dict[str, Any]]:
    """Yield every ``JobPosting`` entity across all shapes in the page.

    Handles a single object, a top-level list, and an ``@graph`` wrapper (and
    an ``@graph`` that is itself nested inside a list).
    """
    for block in _iter_jsonld_blocks(html_text):
        for candidate in _flatten_block(block):
            if _is_job_posting(candidate):
                yield candidate


def _flatten_block(block: Any) -> Iterator[Any]:
    """Expand one parsed block into candidate entities.

    * list  -> each item (recursively, to reach a nested ``@graph``);
    * dict with ``@graph`` -> the graph members (plus the dict itself, since a
      page can legitimately be a JobPosting that also carries an ``@graph``);
    * plain dict -> itself.
    """
    if isinstance(block, list):
        for item in block:
            yield from _flatten_block(item)
    elif isinstance(block, dict):
        yield block
        graph = block.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _flatten_block(item)


def page_has_jobposting_jsonld(html_text: str) -> bool:
    """Cheap predicate: does this HTML embed at least one JobPosting JSON-LD?

    Exposed so the router can gate the JSON-LD tier on already-fetched page
    HTML without constructing a collector. Never raises.
    """
    if not html_text:
        return False
    try:
        for _ in _iter_job_postings(html_text):
            return True
    except Exception:  # pragma: no cover - parser robustness guard
        return False
    return False


def _address_location(address: Any) -> str | None:
    """Flatten a schema.org ``PostalAddress`` into a display string."""
    if isinstance(address, list):
        address = address[0] if address else None
    if isinstance(address, str):
        return clean_text(address)
    if not isinstance(address, dict):
        return None

    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        country if isinstance(country, str) else None,
    ]
    joined = ", ".join(clean_text(p) for p in parts if clean_text(p))
    return joined or None


def _job_location(node: dict[str, Any]) -> str | None:
    """Resolve ``jobLocation`` -> ``address`` -> locality/region/country.

    ``jobLocation`` may be a single ``Place`` dict or a list of them; only the
    first resolvable address is used (job cards rarely need more than one).
    """
    location = node.get("jobLocation")
    candidates = location if isinstance(location, list) else [location]
    for candidate in candidates:
        if isinstance(candidate, dict):
            resolved = _address_location(candidate.get("address"))
            if resolved:
                return resolved
        elif isinstance(candidate, str):
            resolved = clean_text(candidate)
            if resolved:
                return resolved
    return None


def _employment_type(node: dict[str, Any]) -> str | None:
    """schema.org allows ``employmentType`` to be a string or a list."""
    value = node.get("employmentType")
    if isinstance(value, list):
        joined = ", ".join(clean_text(v) for v in value if clean_text(v))
        return joined or None
    return clean_text(value)


class JSONLDCollector(ATSCollector):
    """Generic collector for schema.org JobPosting JSON-LD on any page."""

    provider = JSONLD

    def collect(self) -> CollectionResult:
        if not self.url:
            raise CollectorUnavailable("No URL available for JSON-LD collection")

        try:
            html_text = http_client.get_text(self.url)
        except Exception as exc:  # network/HTTP failure -> let the router escalate
            raise CollectorUnavailable(
                f"JSON-LD page fetch failed for {self.url}: {exc}"
            ) from exc

        rows: list[dict | None] = []
        for node in _iter_job_postings(html_text):
            try:
                rows.append(self._record_from_node(node))
            except Exception as exc:  # one bad entity must not sink the page
                self.log.debug("Skipping malformed JobPosting JSON-LD: %s", exc)
                continue

        finalized = self.finalize(rows)
        if not finalized:
            raise CollectorUnavailable(
                f"No JobPosting JSON-LD found at {self.url}"
            )

        # SEO structured data describes the page it sits on, so a paginated
        # list embeds only the current page's postings. Ask the page whether
        # more exist before letting this count as a company's whole job list.
        total, reason = detect_more_results(html_text, len(finalized), self.url)
        if reason:
            self.log.info(
                "%s: JSON-LD harvested %s row(s) but %s; marking incomplete",
                self.company, len(finalized), reason,
            )
            return CollectionResult(
                jobs=finalized, complete=False, pages_fetched=1,
                reported_total=total, stop_reason=STOP_MORE_AVAILABLE,
            )
        return CollectionResult(
            jobs=finalized, complete=True, pages_fetched=1, reported_total=total,
        )

    def _record_from_node(self, node: dict[str, Any]) -> dict[str, Any] | None:
        job_url = node.get("url") or node.get("@id") or self.url
        # schema.org's canonical property is ``title``, but a meaningful share
        # of real JobPosting blocks carry the posting title under ``name``
        # instead (often when the page templates a generic entity), so fall
        # back to it rather than dropping an otherwise-valid posting.
        return self.record(
            title=node.get("title") or node.get("name"),
            location=_job_location(node),
            date_posted=node.get("datePosted"),
            job_url=job_url,
            employment_type=_employment_type(node),
            description=node.get("description"),
        )
