"""Cornerstone OnDemand (CSOD) careersite collector - token-gated search API.

Cornerstone hosts each customer's careersite on a per-tenant vendor host,
``{tenant}.csod.com`` (e.g. ``jpshealthnet.csod.com``), so
:func:`ats.detector.detect_ats` recognises it from the host alone and sets
``tenant`` to the leading subdomain. A single tenant can expose several
numbered "career sites"; the numeric id lives in the careersite URL path::

    https://{tenant}.csod.com/ux/ats/careersite/{siteId}/home?c={corp}

The job list is served by a JSON search service on the same host::

    POST https://{tenant}.csod.com/services/x/career-site/v1/search
    {"careerSiteId": {siteId}, "cultureId": 1, "cultureName": "en-US",
     "pageNumber": {n}, "pageSize": 100, "searchText": "",
     "cities": [], "states": [], "countryCodes": [], "placeID": "", "radius": 0}

    -> {"status": 0, "data": {"totalCount": 178, "requisitions": [
          {"requisitionId": 30400, "postingEffectiveDate": "8/21/2026",
           "displayJobTitle": "Project Coordinator - Patient Experience",
           "locations": [{"city": "Fort Worth", "state": "TX", "country": "US"}]},
          ...]}}

That endpoint answers ``401`` to an anonymous POST: it requires a short-lived
bearer token. The token is not a login - Cornerstone mints an anonymous
career-site JWT and embeds it in the careersite home page as
``csod.context.token``. We therefore bootstrap by fetching the careersite home
once, lifting the token (and, if the detection did not carry it, the numeric
site id and corp), then paginate the search endpoint with
``Authorization: Bearer {token}`` until ``totalCount`` is exhausted.

The search response is deliberately lean: it carries title, location and
posting date but no employment type or description, so those stay ``None``
rather than triggering a per-requisition detail fetch. The job detail page a
candidate visits is reconstructed from the requisition id::

    https://{tenant}.csod.com/ux/ats/careersite/{siteId}/job/{requisitionId}?c={corp}
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import http_client
from ats.base import ATSCollector, CollectorUnavailable
from normalize import join_location

# Canonical provider name. Mirrors the constants in ats.detector; kept here so
# the collector is importable even where the detector has not yet registered
# the provider.
CORNERSTONE = "cornerstone"

# 100 is the largest page CSOD honours in practice, so a 178-job tenant comes
# back in two requests. MAX_PAGES bounds a tenant that ignores pagination.
PAGE_SIZE = 100
MAX_PAGES = 60

# A tenant can host several numbered career sites; when the URL names none we
# probe this many, keeping every site that returns requisitions. JPS Health, for
# example, splits its openings across sites 2/3/4.
MAX_SITES = 12

# The anonymous career-site JWT the SPA bootstraps with, embedded in the
# careersite home page as ``csod.context = {...};``. The object contains nested
# braces (endpoints, theming, ...), so a non-greedy ``\{.*?\}`` stops at the
# first inner ``}``; we locate the opening brace and match it balanced instead.
_CONTEXT_START_RE = re.compile(r"csod\.context\s*=\s*\{")

# The numeric career-site id in a careersite URL:
# ``/ux/ats/careersite/{siteId}/home`` or ``/.../job/{reqId}``.
_SITE_ID_RE = re.compile(r"/careersite/(\d+)(?:/|$)")


def _extract_context(body: str) -> dict[str, Any] | None:
    """Pull the ``csod.context = {...}`` object via balanced-brace matching.

    Returns the parsed dict, or ``None`` when the marker is absent or the
    braces never balance (truncated page).
    """
    match = _CONTEXT_START_RE.search(body)
    if not match:
        return None
    start = match.end() - 1  # position of the opening '{'
    depth = 0
    for i in range(start, len(body)):
        char = body[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start:i + 1])
                except (ValueError, TypeError):
                    return None
    return None


class CornerstoneCollector(ATSCollector):
    provider = CORNERSTONE

    # -- tenant / host resolution ----------------------------------------
    def _host(self) -> str:
        if self.host:
            return self.host
        if self.url:
            netloc = urlsplit(self.url if "//" in self.url else f"https://{self.url}").netloc
            if netloc:
                return netloc
        if self.tenant:
            return f"{self.tenant}.csod.com"
        raise CollectorUnavailable("No Cornerstone host available")

    def _corp(self, host: str) -> str:
        """The ``c=`` / ``corp`` slug, i.e. the leading label of the host."""
        if self.tenant and "." not in self.tenant:
            return self.tenant
        return host.split(".")[0]

    def _site_id_from_url(self) -> int | None:
        for candidate in (self.url, self.site, self.identifier):
            if not candidate:
                continue
            match = _SITE_ID_RE.search(str(candidate))
            if match:
                return int(match.group(1))
            if str(candidate).isdigit():
                return int(candidate)
        return None

    # -- careersite bootstrap --------------------------------------------
    def _bootstrap(self, host: str, corp: str, site_id: int | None) -> str:
        """Fetch a careersite home once to lift the anonymous JWT.

        The token is an anonymous career-site JWT the SPA needs to call the
        search service; without it the endpoint answers ``401``. Not every
        numbered site renders the context object (inactive sites serve a stub),
        so if the requested site carries no token we retry site 1, which always
        does on a live tenant.
        """
        # Try the URL's site first, then a handful of low-numbered sites - not
        # every site renders the context object (JPS site 1 is a tokenless stub
        # while site 4 carries it), so we probe until one yields a token.
        candidate_sites = [site_id] if site_id is not None else []
        candidate_sites += list(range(1, 6))
        seen: list[int] = []
        for s in candidate_sites:
            if s is not None and s not in seen:
                seen.append(s)

        last_error = "no context token"
        for home_site in seen:
            home_url = f"https://{host}/ux/ats/careersite/{home_site}/home"
            try:
                body = http_client.get_text(home_url, params={"c": corp})
            except Exception as exc:  # network / HTTP failure
                last_error = str(exc)
                continue
            context = _extract_context(body)
            if context and context.get("token"):
                return str(context["token"])
            last_error = "careersite carried no context token"

        raise CollectorUnavailable(f"Cornerstone bootstrap failed: {last_error}")

    # -- record building --------------------------------------------------
    @staticmethod
    def _location(requisition: dict[str, Any]) -> str | None:
        locations = requisition.get("locations")
        if not isinstance(locations, list) or not locations:
            return None
        first = locations[0]
        if not isinstance(first, dict):
            return None
        return join_location(first.get("city"), first.get("state"), first.get("country"))

    def _job_url(self, host: str, corp: str, site_id: int, requisition: dict[str, Any]) -> str | None:
        req_id = requisition.get("requisitionId")
        if req_id is None:
            return None
        return (
            f"https://{host}/ux/ats/careersite/{site_id}/job/{req_id}?c={corp}"
        )

    def _collect_site(
        self, endpoint: str, headers: dict[str, str], host: str, corp: str,
        site_id: int, *, strict: bool,
    ) -> list[dict | None]:
        """Paginate one career site's search endpoint.

        ``strict`` (used when the URL named an explicit site) raises
        :class:`CollectorUnavailable` on a first-page failure; discovery mode
        treats a dead/empty site as simply "no jobs here" and returns ``[]``.
        """
        records: list[dict | None] = []
        total: int | None = None

        for page in range(1, MAX_PAGES + 1):
            payload = {
                "careerSiteId": site_id,
                "cities": [],
                "states": [],
                "countryCodes": [],
                "cultureId": 1,
                "cultureName": "en-US",
                "pageNumber": page,
                "pageSize": PAGE_SIZE,
                "placeID": "",
                "radius": 0,
                "searchText": "",
            }
            try:
                data = http_client.post_json(endpoint, payload, headers=headers)
            except Exception as exc:
                if page == 1 and strict:
                    raise CollectorUnavailable(
                        f"Cornerstone search endpoint unavailable: {exc}"
                    ) from exc
                if page > 1:
                    self.log.warning("%s: Cornerstone site %s page %s failed (%s)",
                                     self.company, site_id, page, exc)
                break

            body = data.get("data") if isinstance(data, dict) else None
            if not isinstance(body, dict):
                if page == 1 and strict:
                    raise CollectorUnavailable("Cornerstone returned no data envelope")
                break

            if total is None:
                total = body.get("totalCount")

            requisitions = body.get("requisitions") or []
            if not requisitions:
                break

            for requisition in requisitions:
                if not isinstance(requisition, dict):
                    continue
                records.append(
                    self.record(
                        title=requisition.get("displayJobTitle") or requisition.get("jobTitle"),
                        location=self._location(requisition),
                        date_posted=requisition.get("postingEffectiveDate"),
                        job_url=self._job_url(host, corp, site_id, requisition),
                        employment_type=None,  # absent from the search response
                        description=None,       # absent from the search response
                    )
                )

            if total is not None and page * PAGE_SIZE >= int(total):
                break

        return records

    # -- interface --------------------------------------------------------
    def collect(self) -> list[dict]:
        host = self._host()
        corp = self._corp(host)
        explicit_site = self._site_id_from_url()

        token = self._bootstrap(host, corp, explicit_site)

        endpoint = f"https://{host}/services/x/career-site/v1/search"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
        }

        records: list[dict | None] = []
        if explicit_site is not None:
            # The URL named a site: trust it and fail loudly if it is broken.
            records = self._collect_site(
                endpoint, headers, host, corp, explicit_site, strict=True,
            )
        else:
            # No site in the URL: probe the numbered sites and keep every one
            # that returns requisitions (a tenant may split roles across sites).
            for site_id in range(1, MAX_SITES + 1):
                records.extend(
                    self._collect_site(
                        endpoint, headers, host, corp, site_id, strict=False,
                    )
                )

        if not records:
            raise CollectorUnavailable("Cornerstone search returned zero requisitions")
        return self.finalize(records)
