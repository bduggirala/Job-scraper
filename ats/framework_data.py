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
from ats.base import ATSCollector, CollectionResult, CollectorUnavailable
from ats.html_utils import make_soup
from normalize import join_location

#: Canonical provider name for records this tier emits.
FRAMEWORK_DATA = "framework_data"

#: ``window.<name> = {...};`` assignments worth parsing.
_ASSIGNMENT_RE = re.compile(
    r"window\.(?:__NUXT__|__INITIAL_STATE__|__APP_STATE__|__PRELOADED_STATE__)"
    r"\s*=\s*(\{.*?\})\s*[;<]",
    re.S,
)

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

    for match in re.finditer(
        r"window\.(?:__NUXT__|__INITIAL_STATE__|__APP_STATE__|__PRELOADED_STATE__)\s*=\s*",
        html_text,
    ):
        brace = html_text.find("{", match.end())
        if brace == -1:
            continue
        blob = _balanced_json(html_text, brace)
        if not blob:
            continue
        try:
            yield json.loads(blob)
        except (ValueError, TypeError):
            continue


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
        # replaying the site's own API, which is a real collector's job.
        return CollectionResult(jobs=jobs, complete=True, pages_fetched=1)
