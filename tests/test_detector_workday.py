from ats.detector import extract_any_embedded_ats_url, extract_embedded_ats_url

# Real shape captured from careers.frostbank.com: the branded page references
# its Workday tenant only through a login link, never a job-board URL.
LOGIN_ONLY_HTML = (
    '<a href="https://frostbank.wd5.myworkdayjobs.com/external/login" '
    'key-href="short-header-shortCandidateloginLinkPath">Login</a>'
)

JOB_URL_HTML = (
    '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External/job/Dallas/'
    'Data-Engineer_R-1234">Data Engineer</a>'
)


def test_login_url_is_not_treated_as_a_job_board():
    assert extract_embedded_ats_url(LOGIN_ONLY_HTML, "workday") is None


def test_any_embedded_url_recovers_the_tenant_from_a_login_link():
    found = extract_any_embedded_ats_url(LOGIN_ONLY_HTML, "workday")
    assert found is not None
    assert "frostbank.wd5.myworkdayjobs.com" in found


def test_any_embedded_url_still_prefers_a_real_job_url():
    found = extract_any_embedded_ats_url(JOB_URL_HTML, "workday")
    assert "acme.wd5.myworkdayjobs.com" in found
