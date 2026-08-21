from ats.detector import detect_ats, extract_any_embedded_ats_url

# Real markup shape captured from jobs.nokia.com/en/sites/CX_1/jobs.
ORACLE_CX_HTML = (
    '<link rel="icon" href="https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com:443'
    '/hcmRestApi/CandidateExperience/siteFavicon/favicon-16x16.png?siteNumber=CX_1&size=16x16">'
)


def test_embedded_oracle_cloud_host_is_extracted():
    found = extract_any_embedded_ats_url(ORACLE_CX_HTML, "taleo")
    assert found is not None
    assert "fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com" in found


def test_extracted_oracle_host_detects_as_taleo():
    found = extract_any_embedded_ats_url(ORACLE_CX_HTML, "taleo")
    assert detect_ats(found)["provider"] == "taleo"


def test_lookalike_host_is_not_extracted():
    html = '<a href="https://evil-oraclecloud.com/phish">careers</a>'
    assert extract_any_embedded_ats_url(html, "taleo") is None
