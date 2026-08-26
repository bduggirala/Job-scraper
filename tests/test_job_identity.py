"""Stable job identity, and the company scoping that keeps it unique.

``job_id`` is the ``jobs`` table primary key, so a collision merges two
employers' postings into one row. That was live: the id is prefixed with
``ats_provider``, which is the literal string "unknown" for every one of the
89 browser-routed companies, so any two of them sharing a numeric id in their
URLs produced the same id. All four extraction strategies were affected.
"""

import pytest

from job_identity import extract_stable_job_id as jid


# --- the collision, per extraction strategy --------------------------------

@pytest.mark.parametrize("url_a,url_b,label", [
    ("https://careers.acme.com/jobs/1043321",
     "https://jobs.contoso.com/openings/1043321", "trailing numeric"),
    ("https://acme.com/careers?jobId=55512",
     "https://contoso.com/apply?jobid=55512", "query jobId"),
    ("https://acme.com/x?reqId=RQ88",
     "https://contoso.com/y?reqid=RQ88", "query reqId"),
    ("https://acme.com/j/3f2504e0-4f89-11d3-9a0c-0305e82c3301",
     "https://contoso.com/p/3f2504e0-4f89-11d3-9a0c-0305e82c3301", "trailing uuid"),
])
def test_two_companies_sharing_an_id_do_not_collide(url_a, url_b, label):
    assert jid(url_a, "unknown", "Acme Corp") != jid(url_b, "unknown", "Contoso Ltd"), (
        f"{label}: distinct companies produced the same job_id"
    )


def test_the_same_job_keeps_one_id_across_runs():
    url = "https://careers.acme.com/jobs/1043321"
    assert jid(url, "unknown", "Acme Corp") == jid(url, "unknown", "Acme Corp")


def test_a_retitled_posting_keeps_its_id():
    """The original point of stable ids: a slug change is not a new job."""
    before = jid(
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Dallas/Data-Engineer_R246063",
        "workday", "Acme Corp",
    )
    after = jid(
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Dallas/Data-Engineer-II_R246063",
        "workday", "Acme Corp",
    )
    assert before == after


def test_company_naming_variants_resolve_to_the_same_scope():
    """Workbook drift ("Acme Inc" vs "Acme, Inc.") must not orphan a company."""
    url = "https://careers.acme.com/jobs/1043321"
    assert jid(url, "unknown", "Acme Inc") == jid(url, "unknown", "Acme, Inc.")


def test_one_url_is_one_job_whatever_route_reached_it():
    """Scheme v3: a generically-extracted id no longer carries the provider.

    This replaces an earlier assertion that the same URL under two provider
    labels was two different jobs. That premise was wrong - one URL is one
    posting - and it had a real cost: ``ats_provider`` is the literal string
    "unknown" for every browser-routed row, so a company that fell back to
    Playwright re-keyed its entire job list, reported all of it as new, and
    aged out the API-keyed copies of the very same requisitions.
    """
    url = "https://acme.com/careers?jobId=1"
    assert jid(url, "workday", "Acme") == jid(url, "icims", "Acme")
    assert jid(url, "taleo", "Acme") == jid(url, "unknown", "Acme")


def test_the_same_id_at_two_employers_stays_distinct():
    """What the provider label was really protecting: cross-company collisions.

    The company scope does this job, and does it better - the label could not
    separate two employers whose rows were both labelled "unknown".
    """
    assert (
        jid("https://a.com/careers?jobId=55512", "unknown", "Alpha Foods")
        != jid("https://b.com/apply?jobid=55512", "unknown", "Beta Health")
    )


def test_a_missing_url_still_yields_an_empty_id():
    assert jid(None, "workday", "Acme") == ""


def test_the_url_fallback_is_also_company_scoped():
    """Even the last-resort URL fallback must not be shared between companies."""
    url = "https://jobs.example.com/careers/data-engineer"
    assert jid(url, "unknown", "Acme") != jid(url, "unknown", "Contoso")
