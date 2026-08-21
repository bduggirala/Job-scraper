from ats.discovery import candidates_from_html, careers_links, root_domain_url

ORACLE_PAGE = (
    '<html><body>'
    '<link href="https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com:443'
    '/hcmRestApi/CandidateExperience/siteFavicon/favicon-16x16.png?siteNumber=CX_1">'
    '</body></html>'
)

MARKETING_PAGE = (
    '<html><body>'
    '<a href="/about">About us</a>'
    '<a href="/careers/search">Search jobs</a>'
    '<a href="/en/openings">Current openings</a>'
    '<a href="https://twitter.com/acme">Twitter</a>'
    '</body></html>'
)

NOTHING_PAGE = '<html><body><p>We are hiring soon.</p></body></html>'


def test_root_domain_strips_subdomain_and_path():
    assert root_domain_url("https://careers.frostbank.com/us/en") == "https://frostbank.com"


def test_root_domain_handles_bare_host():
    assert root_domain_url("https://acme.com") == "https://acme.com"


def test_root_domain_returns_none_for_garbage():
    assert root_domain_url("not a url") is None


def test_candidates_finds_embedded_oracle_host():
    found = candidates_from_html(ORACLE_PAGE, "https://jobs.nokia.com/en/sites/CX_1/jobs")
    assert any("oraclecloud.com" in c for c in found)


def test_candidates_empty_when_page_names_no_ats():
    assert candidates_from_html(NOTHING_PAGE, "https://acme.com/careers") == []


def test_careers_links_prefers_job_list_hrefs():
    links = careers_links(MARKETING_PAGE, "https://acme.com/careers")
    assert "https://acme.com/careers/search" in links
    assert "https://acme.com/en/openings" in links


def test_careers_links_ignores_unrelated_links():
    links = careers_links(MARKETING_PAGE, "https://acme.com/careers")
    assert not any("twitter.com" in link for link in links)
    assert not any(link.endswith("/about") for link in links)


def test_careers_links_respects_limit():
    assert len(careers_links(MARKETING_PAGE, "https://acme.com/careers", limit=1)) == 1
