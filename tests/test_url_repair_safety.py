"""A transient DNS blip must not permanently rewrite a working URL.

``repair_careers_url`` declares a host dead on a single failed
``getaddrinfo``, and the README documents that Chromium's resolver buckles
under concurrent browser instances - the same conditions that make a lookup
fail spuriously. A repair that then finds *any* page passing the very loose
"looks like a careers page" test gets written back over the original.

Two guards: confirm a non-resolving host with a second lookup, and require the
replacement page to contain actual job links rather than three generic words.
"""

import pytest

import ats.url_repair as url_repair


@pytest.fixture(autouse=True)
def _clear_dns_cache():
    url_repair.host_resolves.cache_clear()
    yield
    url_repair.host_resolves.cache_clear()


def test_a_host_failing_once_then_resolving_is_not_declared_dead(monkeypatch):
    """The flaky-resolver case: one failure is not evidence of a dead host."""
    attempts = {"n": 0}

    def flaky(host):
        attempts["n"] += 1
        return attempts["n"] > 1        # fails first, succeeds after

    monkeypatch.setattr(url_repair, "_resolves_once", flaky)

    assert url_repair.host_resolves("careers.acme.com") is True
    assert attempts["n"] >= 2, "declared a host dead on a single lookup"


def test_a_host_failing_every_time_is_declared_dead(monkeypatch):
    monkeypatch.setattr(url_repair, "_resolves_once", lambda host: False)
    assert url_repair.host_resolves("careers.dead.example") is False


def test_a_live_host_is_confirmed_on_the_first_lookup(monkeypatch):
    """No extra lookups for the overwhelmingly common case."""
    attempts = {"n": 0}

    def always(host):
        attempts["n"] += 1
        return True

    monkeypatch.setattr(url_repair, "_resolves_once", always)

    assert url_repair.host_resolves("careers.acme.com") is True
    assert attempts["n"] == 1


# --- what counts as a careers page ----------------------------------------

CORPORATE_HOMEPAGE = """
<html><body>
  <h1>Acme builds things</h1>
  <p>Our position in the market is strong. See our job creation record and
     read about careers at Acme in our annual report. Vacancies in leadership
     are rare. Apply now to our newsletter.</p>
</body></html>""" + ("<p>filler</p>" * 200)

REAL_CAREERS_PAGE = """
<html><body>
  <h1>Open roles</h1>
  <a href="/careers/job/1001">Senior Data Engineer</a>
  <a href="/careers/job/1002">Data Platform Engineer</a>
  <a href="/careers/job/1003">Analytics Engineer</a>
</body></html>""" + ("<p>filler</p>" * 200)


def test_a_corporate_homepage_is_not_accepted_as_a_careers_page():
    """Three generic word hits was the entire old test, and almost every
    corporate page contains 'job', 'career' and 'position'."""
    assert url_repair._looks_like_careers_page(CORPORATE_HOMEPAGE) is False


def test_a_page_with_real_job_links_is_accepted():
    assert url_repair._looks_like_careers_page(REAL_CAREERS_PAGE) is True


def test_a_tiny_page_is_never_accepted():
    assert url_repair._looks_like_careers_page("<html></html>") is False
