"""Offline tests for embedded-ATS-URL extraction, particularly the
last-resort ``extract_any_embedded_ats_url`` fallback.

Confirmed against HCLTech's careers page: its only path-bearing
SuccessFactors reference is a shared jQuery asset host
(``hcm55.sapsf.eu/.../jquery.js``); the real tenant search lives on the
sibling ``career55.sapsf.eu`` host, which the page references without a
path (so it never matches the extraction regex at all). Before this fix,
the fallback returned the asset URL, and driving the SuccessFactors
collector from its host 404s every time.
"""

from ats.detector import SUCCESSFACTORS, extract_any_embedded_ats_url, extract_embedded_ats_url

HCLTECH_LIKE_HTML = """
<html><head>
<script src="https://hcm55.sapsf.eu/verp/vmod_v1/ui/extlib/jquery_3.5.1/jquery.js"></script>
<script src="https://hcm55.sapsf.eu/verp/vmod_v1/ui/extlib/jquery_3.5.1/jquery-migrate.js"></script>
</head><body>Careers</body></html>
"""


def test_embedded_extractor_rejects_asset_url():
    assert extract_embedded_ats_url(HCLTECH_LIKE_HTML, SUCCESSFACTORS) is None


def test_any_embedded_extractor_also_rejects_asset_only_page():
    """The last-resort fallback must not fall back to a .js asset host."""
    assert extract_any_embedded_ats_url(HCLTECH_LIKE_HTML, SUCCESSFACTORS) is None


def test_any_embedded_extractor_still_accepts_non_asset_login_link():
    """Guards the original frostbank.com use case: a /login path is fine."""
    html = '<a href="https://frostbank.wd5.myworkdayjobs.com/external/login">Sign in</a>'
    from ats.detector import WORKDAY
    result = extract_any_embedded_ats_url(html, WORKDAY)
    assert result == "https://frostbank.wd5.myworkdayjobs.com/external/login"
