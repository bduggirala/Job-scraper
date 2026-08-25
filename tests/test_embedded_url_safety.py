"""Embedded-ATS-URL extraction must not return assets, legal pages or
private-network hosts.

Two live failures motivated this. HCLTech's stored ATS URL was a CDN *image*
(``rmkcdn.successfactors.com/....png``) and Builders FirstSource's was iCIMS's
own privacy notice - both written into the workbook by
``extract_any_embedded_ats_url``, whose fallback loop rejected only ``.js``,
``.css`` and ``.map`` while skipping the asset/legal filters its strict
sibling applies.

The SSRF half is the same function viewed differently: these URLs come from
third-party pages, are driven through a collector, and are then persisted and
re-fetched on later runs.
"""

import pytest

from ats.detector import (
    ICIMS,
    SUCCESSFACTORS,
    WORKDAY,
    extract_all_embedded_ats_urls,
    extract_any_embedded_ats_url,
    extract_embedded_ats_url,
    is_safe_fetch_target,
)


# --- F-31: assets and legal pages are not ATS endpoints --------------------

def test_a_cdn_image_is_never_returned_as_an_ats_url():
    """The live HCLTech failure."""
    html = ('<html><img src="https://rmkcdn.successfactors.com/'
            '147eb21f/e4749fe5-51ba-4e05-b5f0-c.png"></html>')
    assert extract_any_embedded_ats_url(html, SUCCESSFACTORS) is None


def test_a_legal_page_is_never_returned_as_an_ats_url():
    """The live Builders FirstSource failure."""
    html = ('<html><a href="https://www.icims.com/legal/privacy-notice-website/">'
            'Privacy</a></html>')
    assert extract_any_embedded_ats_url(html, ICIMS) is None


@pytest.mark.parametrize("asset", [
    "https://cdn.successfactors.com/a/logo.png",
    "https://static.successfactors.com/a/hero.jpg",
    "https://assets.successfactors.com/a/icon.svg",
    "https://rmkcdn.successfactors.com/a/font.woff2",
    "https://media.successfactors.com/a/brochure.pdf",
])
def test_shared_cdn_hosts_are_rejected_whatever_they_serve(asset):
    """The host is the signal, not the extension - a CDN shared across every
    customer carries no tenant coordinates a collector could use."""
    assert extract_any_embedded_ats_url(f'<html><img src="{asset}"></html>',
                                        SUCCESSFACTORS) is None


def test_a_script_asset_is_rejected_even_on_a_tenant_shaped_host():
    """HCLTech's only path-bearing SF reference is jquery.js on hcm55.sapsf.eu,
    a script host shared across tenants - its real search lives on the sibling
    career55.sapsf.eu, and driving the script host 404s every time."""
    html = '<html><script src="https://hcm55.sapsf.eu/extlib/jquery.js"></script></html>'
    assert extract_any_embedded_ats_url(html, SUCCESSFACTORS) is None


def test_a_favicon_on_a_tenant_host_still_yields_the_tenant():
    """The counter-case that makes 'reject all assets' wrong: jobs.nokia.com
    names its Oracle tenant only through a favicon, and driving the API from
    that host returns 575 jobs."""
    from ats.detector import TALEO
    html = ('<link rel="icon" href="https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com:443'
            '/hcmRestApi/CandidateExperience/siteFavicon/favicon-16x16.png?siteNumber=CX_1">')
    found = extract_any_embedded_ats_url(html, TALEO)
    assert found is not None
    assert "fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com" in found


def test_the_login_link_case_the_fallback_exists_for_still_works():
    """careers.frostbank.com references its Workday tenant only via /login.

    That is the whole reason extract_any_embedded_ats_url exists: the URL is
    not a job board, but its host carries the tenant/site the collector needs.
    Tightening the filters must not break it.
    """
    html = ('<html><a href="https://frostbank.wd5.myworkdayjobs.com/'
            'External/login">Current employees</a></html>')
    found = extract_any_embedded_ats_url(html, WORKDAY)
    assert found is not None
    assert "frostbank.wd5.myworkdayjobs.com" in found


def test_a_real_job_board_url_is_still_preferred_over_a_login_link():
    html = ('<html>'
            '<a href="https://acme.wd5.myworkdayjobs.com/External/login">Sign in</a>'
            '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Jobs</a>'
            '</html>')
    assert extract_any_embedded_ats_url(html, WORKDAY).endswith("/en-US/External")


# --- F-18: SSRF guard on URLs harvested from third-party pages -------------

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata
    "https://127.0.0.1/admin",
    "https://localhost/internal",
    "https://10.0.0.5/jobs",
    "https://192.168.1.10/careers",
    "https://172.16.4.4/careers",
    "https://[::1]/jobs",
    "file:///etc/passwd",
    "ftp://example.com/jobs",
])
def test_private_and_non_http_targets_are_refused(url):
    assert is_safe_fetch_target(url) is False


@pytest.mark.parametrize("url", [
    "https://acme.wd5.myworkdayjobs.com/en-US/External",
    "https://boards.greenhouse.io/acme",
    "http://careers.example.com/jobs",
])
def test_ordinary_public_career_urls_are_allowed(url):
    assert is_safe_fetch_target(url) is True


def test_a_private_host_embedded_in_a_page_is_not_extracted():
    """The persistence path is what makes this matter: a discovered URL is
    written into the workbook and re-fetched on every later run."""
    html = '<html><a href="https://10.0.0.5.icims.com/jobs/search">Jobs</a></html>'
    found = extract_all_embedded_ats_urls(html, ICIMS)
    assert all(is_safe_fetch_target(u) for u in found)
