"""Job data embedded in a JavaScript framework's hydration payload.

Single-page careers sites ship their data twice: once as the JSON the client
framework hydrates from, and once as DOM the browser paints. Reading the first
costs one GET; rendering the second costs a Chromium instance.

Three payload shapes cover most of what turns up:

* ``<script id="__NEXT_DATA__">`` - Next.js, clean JSON in a script tag;
* ``window.__NUXT__ = {...}`` - Nuxt;
* ``window.__INITIAL_STATE__ = {...}`` - Vue/Redux and several house frameworks.

:mod:`ats.phenom` already proved the pattern works (``phApp.ddo`` is the same
idea under a vendor-specific name); this generalises it rather than waiting for
each new site to earn its own module.

The payload shape is not known in advance, so the tree is walked for
*job-shaped* objects: a dict carrying a title-ish key and a URL-ish key. That
is deliberately conservative - a page whose payload holds articles or products
must escalate to the browser rather than emit nonsense rows.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

import http_client
from ats.base import (
    ATSCollector,
    CollectionResult,
    CollectorUnavailable,
    STOP_MORE_AVAILABLE,
)
from ats.html_utils import detect_more_results, make_soup
from normalize import join_location

#: Canonical provider name for records this tier emits.
FRAMEWORK_DATA = "framework_data"

#: ``window.<name> = {...};`` assignments worth parsing.
#:
#: The four framework-standard names are tried first because they are the ones
#: that reliably hold hydration state. But a house-built careers site names its
#: payload whatever it likes - Paylocity ships the entire board as
#: ``window.pageData`` - and a fixed list of four could only ever see the sites
#: that happened to use a framework convention. So an assignment to *any*
#: plausible window property is a candidate now, with the walk's own
#: job-shape test doing the discriminating (see :func:`_is_job_shaped`).
_ASSIGNMENT_RE = re.compile(
    r"window\.(?:__NUXT__|__INITIAL_STATE__|__APP_STATE__|__PRELOADED_STATE__)"
    r"\s*=\s*(\{.*?\})\s*[;<]",
    re.S,
)

#: The named payloads, tried before anything else.
_KNOWN_ASSIGNMENT_RE = re.compile(
    r"window\.(?:__NUXT__|__INITIAL_STATE__|__APP_STATE__|__PRELOADED_STATE__)\s*=\s*"
)

#: Any other ``window.<identifier> = {`` on the page. Deliberately permissive
#: about the name and strict about everything after it: the blob must parse as
#: JSON and must contain job-shaped objects before a single row is emitted.
_ANY_ASSIGNMENT_RE = re.compile(r"window\.([A-Za-z_$][\w$]*)\s*=\s*(?=\{)")

#: Analytics and consent payloads sit in exactly this shape on nearly every
#: careers page. Skipping them by name is cheaper than parsing a 100 KB
#: tag-manager blob to discover it holds no jobs.
_IGNORED_ASSIGNMENTS = frozenset({
    "datalayer", "dataLayer", "ga", "gtag", "_gaq", "utag_data", "digitalData",
    "adobeDataLayer", "optimizely", "onetrust", "otconsent", "clarity",
    "__reactdevtools_global_hook__", "performance", "config", "settings",
})

#: How many unnamed candidates to parse per page. A page can carry dozens of
#: small window assignments; balanced-brace scanning each one is linear in the
#: page size, so the total stays bounded rather than quadratic on a hostile page.
_MAX_CANDIDATES = 25

#: Below this, a blob is a feature flag or a config stub, not a job list.
_MIN_BLOB_CHARS = 200

#: Keys that carry a posting's title, URL, location and date, most specific
#: first. Matched case-insensitively against a candidate dict's keys.
_TITLE_KEYS = ("title", "jobtitle", "name", "positiontitle", "displayjobtitle")
_URL_KEYS = ("url", "joburl", "applyurl", "canonicalurl", "link", "href", "absolute_url")
_LOCATION_KEYS = ("location", "city", "joblocation", "primarylocation",
                  "locationname", "cityStateCountry", "full_location")
_DATE_KEYS = ("dateposted", "posteddate", "publisheddate", "createdat",
              "postedon", "startdate", "first_published")

#: A payload can be enormous; walking it without a bound risks pathological
#: recursion on a hostile or generated page.
_MAX_NODES = 200_000


def _balanced_json(text: str, start: int) -> str | None:
    """Extract the balanced ``{...}`` beginning at ``start``.

    A non-greedy regex stops at the first inner brace, which for a hydration
    payload is almost immediately.
    """
    depth = 0
    for i in range(start, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _payloads(html_text: str) -> Iterator[Any]:
    """Yield every framework payload the page carries, parsed."""
    soup = make_soup(html_text)
    for script in soup.find_all("script", attrs={"id": "__NEXT_DATA__"}):
        raw = script.string or script.get_text()
        if raw:
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                continue

    seen_at: set[int] = set()

    for match in _KNOWN_ASSIGNMENT_RE.finditer(html_text):
        brace = html_text.find("{", match.end())
        if brace == -1 or brace in seen_at:
            continue
        seen_at.add(brace)
        # No size floor here: a named hydration payload is worth reading at
        # any size, and applying the speculative-candidate floor to it would
        # silently change what this tier already handled.
        payload = _parse_blob(html_text, brace, min_chars=0)
        if payload is not None:
            yield payload

    # Then anything else assigned to a window property. Ordered after the
    # known names so a page carrying both is read from its real hydration
    # state first.
    budget = _MAX_CANDIDATES
    for match in _ANY_ASSIGNMENT_RE.finditer(html_text):
        if budget <= 0:
            break
        if match.group(1).lower() in _IGNORED_ASSIGNMENTS:
            continue
        brace = html_text.find("{", match.end() - 1)
        if brace == -1 or brace in seen_at:
            continue
        seen_at.add(brace)
        budget -= 1
        payload = _parse_blob(html_text, brace)
        if payload is not None:
            yield payload


def _parse_blob(html_text: str, brace: int, min_chars: int = _MIN_BLOB_CHARS) -> Any | None:
    """Balanced-scan and parse one ``{...}`` payload, or None."""
    blob = _balanced_json(html_text, brace)
    if not blob or len(blob) < min_chars:
        return None
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        return None


def _pick(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {str(k).lower(): v for k, v in node.items()}
    for key in keys:
        value = lowered.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _is_job_shaped(node: Any) -> bool:
    """A dict carrying both a title-ish and a URL-ish key.

    Requiring both is what keeps articles, products and nav entries out: a
    payload full of ``{"name": ..., "price": ...}`` has no URL key and a
    breadcrumb has no title key.
    """
    if not isinstance(node, dict):
        return False
    return _pick(node, _TITLE_KEYS) is not None and _pick(node, _URL_KEYS) is not None


def _walk(node: Any, budget: list[int]) -> Iterator[dict[str, Any]]:
    budget[0] -= 1
    if budget[0] <= 0:
        return
    if isinstance(node, dict):
        if _is_job_shaped(node):
            yield node
        for value in node.values():
            yield from _walk(value, budget)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, budget)


class FrameworkDataCollector(ATSCollector):
    """Read a framework hydration payload instead of rendering the page."""

    provider = FRAMEWORK_DATA

    def collect(self) -> CollectionResult:
        if not self.url:
            raise CollectorUnavailable("No URL available for framework-data collection")

        try:
            html_text = http_client.get_text(
                self.url, headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except Exception as exc:
            raise CollectorUnavailable(
                f"Framework-data fetch failed for {self.url}: {exc}"
            ) from exc

        seen: set[str] = set()
        records: list[dict | None] = []
        for payload in _payloads(html_text):
            for node in _walk(payload, [_MAX_NODES]):
                url = _pick(node, _URL_KEYS)
                if not isinstance(url, str) or url in seen:
                    continue
                seen.add(url)

                location = _pick(node, _LOCATION_KEYS)
                if isinstance(location, dict):
                    location = join_location(
                        location.get("city"), location.get("state"),
                        location.get("country"),
                    )
                elif isinstance(location, list):
                    location = join_location(*[str(x) for x in location[:3]])

                records.append(self.record(
                    title=_pick(node, _TITLE_KEYS),
                    location=location,
                    date_posted=_pick(node, _DATE_KEYS),
                    job_url=url,
                ))

        jobs = self.finalize([r for r in records if r])
        if not jobs:
            raise CollectorUnavailable(
                f"No framework job payload found at {self.url}"
            )

        # One document, whatever it hydrated with. Pagination here would mean
        # replaying the site's own API, which is a real collector's job - so
        # when the document itself says there are more results than it
        # hydrated, say so rather than reporting page one as the whole list.
        total, reason = detect_more_results(html_text, len(jobs), self.url)
        if reason:
            self.log.info("%s: framework payload carried %s row(s) but %s; "
                     "marking incomplete", self.company, len(jobs), reason)
            return CollectionResult(
                jobs=jobs, complete=False, pages_fetched=1,
                reported_total=total, stop_reason=STOP_MORE_AVAILABLE,
            )
        return CollectionResult(
            jobs=jobs, complete=True, pages_fetched=1, reported_total=total,
        )


class JsonEndpointCollector(FrameworkDataCollector):
    """Collect from a JSON list endpoint remembered by :mod:`browser_hints`.

    Same walker as its parent, different source: the parent digs a hydration
    payload out of an HTML document, while this one is handed the API URL that
    document's JavaScript was calling. The browser found that URL by watching
    network traffic; reading it directly is what lets the company stop needing
    a browser at all.

    The job-shaped test is doing real work here. A remembered endpoint is only
    ever a *candidate* - it was recorded because it repeated with varying
    parameters, not because anyone confirmed it serves jobs - so an endpoint
    that turns out to return facets, filters or telemetry raises
    ``CollectorUnavailable`` and the caller falls back to the browser.
    """

    def collect(self) -> CollectionResult:
        if not self.url:
            raise CollectorUnavailable("No endpoint URL for JSON collection")

        try:
            text = http_client.get_text(
                self.url, headers={"Accept": "application/json"},
            )
        except Exception as exc:
            raise CollectorUnavailable(
                f"Hinted endpoint fetch failed for {self.url}: {exc}"
            ) from exc

        try:
            payload = json.loads(text)
        except Exception as exc:
            raise CollectorUnavailable(
                f"Hinted endpoint did not return JSON: {exc}"
            ) from exc

        seen: set[str] = set()
        records: list[dict | None] = []
        for node in _walk(payload, [_MAX_NODES]):
            url = _pick(node, _URL_KEYS)
            if not isinstance(url, str) or url in seen:
                continue
            seen.add(url)

            location = _pick(node, _LOCATION_KEYS)
            if isinstance(location, dict):
                location = join_location(
                    location.get("city"), location.get("state"),
                    location.get("country"),
                )
            elif isinstance(location, list):
                location = join_location(*[str(x) for x in location[:3]])

            records.append(self.record(
                title=_pick(node, _TITLE_KEYS),
                location=location,
                date_posted=_pick(node, _DATE_KEYS),
                job_url=url,
            ))

        jobs = self.finalize([r for r in records if r])
        if not jobs:
            raise CollectorUnavailable(
                f"Hinted endpoint carried no job-shaped rows: {self.url}"
            )
        # One page of whatever the endpoint returned. Walking its pagination
        # would mean reverse-engineering an unknown API; the honest report is
        # that this is a page, not necessarily the whole list.
        return CollectionResult(
            jobs=jobs, complete=True, pages_fetched=1,
        )
