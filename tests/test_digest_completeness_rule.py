"""Not every incomplete scrape should silence the digest.

The original rule was "every company completed", which is right for the case
it was written for - a page that *failed* mid-pagination leaves an unknown,
effectively random hole, so what looks new might just be what we happened to
reach this time.

But it is wrong for the other kind of incompleteness. CVS Health lists 19,246
postings and Phenom serves ten per request; collecting all of them is 1,925
sequential requests, past any sane per-company timeout. That company will be
truncated on every run forever, and under the old rule it would silence every
digest forever - which turns one known limitation into total loss of alerting.

The distinction that matters is whether we know *what* we missed. A budget
truncation walks newest-first, so the gap is the oldest postings, and nothing
inside a 7-day freshness window is behind it. A failed page is a hole of
unknown shape.
"""

import pytest

from ats.base import STOP_BUDGET, STOP_PAGE_FAILED
from notify import should_send


def _job():
    return {"job_id": "a", "company": "Acme", "title": "Data Engineer"}


def test_a_failed_page_still_silences_the_digest():
    """Unknown hole: what looks new might just be what we reached."""
    assert should_send(
        new_jobs=[_job()], changed_jobs=[], run_complete=False,
        stop_reasons={STOP_PAGE_FAILED},
    ) is False


def test_a_budget_truncation_does_not_silence_the_digest():
    """Known hole, and it is the oldest postings - newest-first ordering
    means nothing inside the freshness window sits behind it."""
    assert should_send(
        new_jobs=[_job()], changed_jobs=[], run_complete=False,
        stop_reasons={STOP_BUDGET},
    ) is True


def test_a_failed_page_anywhere_wins_over_a_budget_truncation():
    """Mixed run: the untrustworthy one decides."""
    assert should_send(
        new_jobs=[_job()], changed_jobs=[], run_complete=False,
        stop_reasons={STOP_BUDGET, STOP_PAGE_FAILED},
    ) is False


def test_a_complete_run_still_sends():
    assert should_send(
        new_jobs=[_job()], changed_jobs=[], run_complete=True, stop_reasons=set(),
    ) is True


def test_nothing_new_still_sends_nothing():
    assert should_send(
        new_jobs=[], changed_jobs=[], run_complete=True, stop_reasons=set(),
    ) is False


def test_the_rule_defaults_to_the_strict_behaviour():
    """Callers that say nothing about reasons keep the cautious rule."""
    assert should_send(new_jobs=[_job()], changed_jobs=[], run_complete=False) is False
