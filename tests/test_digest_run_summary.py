"""What the digest says about the run that produced it.

The digest listed the jobs and then two numbers - companies scanned, jobs
collected - which is enough to read the list but not enough to trust it. A
reader seeing 3 new jobs cannot tell whether that is 3 out of a clean sweep of
180 companies or 3 out of the 40 that happened not to time out, and those two
runs deserve very different reactions.

So the summary block carries the four outcomes separately, and names the run.
``partial`` and ``blocked`` matter most: a partial company's absent jobs were
never confirmed absent, and a blocked one was never read at all.
"""

from notify import build_digest


def _job(title="Data Engineer"):
    return {"company": "Acme", "title": title, "location": "Dallas, TX",
            "job_url": "https://acme.test/jobs/1", "remote_scope": "onsite"}


def _summary(**over):
    base = {
        "run_id": "20260826T101500Z",
        "companies_scanned": 180,
        "companies_successful": 150,
        "companies_partial": 12,
        "companies_failed": 15,
        "companies_blocked": 3,
        "jobs_collected": 120_003,
        "new_jobs": 4,
        "changed_jobs": 2,
    }
    base.update(over)
    return base


def test_the_digest_names_the_run_it_came_from():
    digest = build_digest([_job()], [], _summary())
    assert "20260826T101500Z" in digest.text
    assert "20260826T101500Z" in digest.html


def test_the_digest_reports_all_four_company_outcomes():
    digest = build_digest([_job()], [], _summary())

    for label, value in (("success", "150"), ("partial", "12"),
                         ("failed", "15"), ("blocked", "3")):
        assert label in digest.text.lower(), f"{label} count is missing"
        assert value in digest.text, f"the {label} count {value} is missing"


def test_the_digest_reports_how_many_companies_were_attempted():
    assert "180" in build_digest([_job()], [], _summary()).text


def test_the_digest_reports_the_jobs_fetched_and_the_new_and_changed_counts():
    digest = build_digest([_job()], [_job("Senior Data Engineer")], _summary())

    assert "120,003" in digest.text
    assert "1 new" in digest.subject
    assert "1 changed" in digest.subject


def test_a_run_with_nothing_new_says_so_rather_than_going_blank():
    """build_digest is reachable with an empty set (a summary-only send), and
    an email whose body is just a horizontal rule reads as a broken template."""
    digest = build_digest([], [], _summary(new_jobs=0, changed_jobs=0))

    assert "no new" in digest.text.lower()
    assert "no new" in digest.html.lower()


def test_a_clean_run_does_not_invent_a_truncation_warning():
    digest = build_digest([_job()], [], _summary(
        companies_partial=0, companies_blocked=0, companies_failed=0))

    assert "removal" not in digest.text.lower()


def test_a_partial_run_warns_that_removals_were_not_synced():
    digest = build_digest([_job()], [], _summary(companies_partial=12))
    assert "removal" in digest.text.lower()


def test_the_summary_block_survives_a_caller_that_omits_the_new_fields():
    """Older callers pass only companies_scanned / jobs_collected."""
    digest = build_digest([_job()], [], {
        "companies_scanned": 9, "jobs_collected": 6084})

    assert "9" in digest.text and "6,084" in digest.text
