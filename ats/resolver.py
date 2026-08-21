"""Page-level ATS resolution for branded career sites.

Roughly 80% of the input workbook lists a branded careers URL
(``careers.acme.com``) rather than an ATS URL. Most of those pages redirect to
or embed a supported ATS. Fetching the page once and fingerprinting it turns a
large share of would-be Playwright companies into cheap direct-API companies.

This is the "detect known ATS / API" step of the router's Live-Jobs-Page
branch. It performs exactly one HTTP GET per company and never executes
JavaScript - if the page reveals nothing, the router falls back to Playwright.
"""

from __future__ import annotations

from typing import Any

import http_client
from ats.detector import (
    UNKNOWN,
    detect_ats,
    detect_from_html,
    extract_embedded_ats_url,
)
from logger import get_logger

log = get_logger("ats.resolver")

# Only read the first chunk of the page; ATS fingerprints appear in <head>
# or early script tags, and some career sites are multi-megabyte.
MAX_BYTES = 600_000


def resolve_from_page(company: str, url: str) -> dict[str, Any]:
    """Fetch ``url`` and try to identify the ATS behind it.

    Returns a detection dict (same shape as :func:`ats.detector.detect_ats`).
    ``provider`` is ``"unknown"`` when the page yields no usable signal.
    """
    empty = {
        "provider": UNKNOWN,
        "url": url,
        "identifier": None,
        "host": None,
        "tenant": None,
        "site": None,
    }

    try:
        response = http_client.request(url, method="GET", allow_redirects=True, stream=True)
    except Exception as exc:
        log.debug("%s: page resolution failed for %s (%s)", company, url, exc)
        return empty

    try:
        final_url = str(response.url)
        # The redirect target alone often identifies the ATS.
        redirected = detect_ats(final_url)
        if redirected["provider"] != UNKNOWN:
            log.debug("%s: resolved via redirect -> %s", company, redirected["provider"])
            return redirected

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type and "json" not in content_type and "text" not in content_type:
            return empty

        body = response.raw.read(MAX_BYTES, decode_content=True) or b""
        html_text = body.decode(response.encoding or "utf-8", errors="replace")
    except Exception as exc:
        log.debug("%s: could not read page body for %s (%s)", company, url, exc)
        return empty
    finally:
        response.close()

    provider = detect_from_html(html_text, final_url=final_url)
    if provider == UNKNOWN:
        return empty

    # Prefer a concrete embedded ATS URL so the collector gets real
    # tenant/site coordinates rather than the branded host.
    embedded = extract_embedded_ats_url(html_text, provider)
    if embedded:
        detection = detect_ats(embedded)
        if detection["provider"] != UNKNOWN:
            log.debug("%s: resolved via embedded URL -> %s (%s)", company, provider, embedded)
            return detection

    # Fingerprint matched but no explicit URL: keep the branded host, which is
    # correct for host-based providers such as Phenom and Eightfold.
    detection = detect_ats(final_url)
    detection["provider"] = provider
    if not detection.get("host"):
        detection["host"] = final_url.split("/")[2] if "//" in final_url else None
    log.debug("%s: resolved via fingerprint -> %s", company, provider)
    return detection
