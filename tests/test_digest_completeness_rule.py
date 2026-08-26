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


from ats.base import (
    STOP_BUDGET,
    STOP_PAGE_CEILING,
    STOP_PAGE_FAILED,
    STOP_SHORT_OF_TOTAL,
)
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


def test_the_page_ceiling_is_the_same_kind_of_hole_as_the_job_budget():
    """Found by running the whole workbook: the digest could never send.

    ``page_ceiling`` was added after this rule was written and never added to
    it, so any run touching it was treated as untrustworthy. CVS Health and
    Signify Health each list 19,254 postings against a provider serving ten per
    request, so both trip the 500-page ceiling on *every* run and always will -
    which silenced the digest permanently, the exact outcome the budget carve-
    out above exists to prevent. A real run confirmed it:

        Run incomplete for reasons that make 'new' untrustworthy
        (budget_exhausted, page_ceiling, short_of_reported_total);
        suppressing the digest

    Both are ceilings *we* impose on a newest-first walk. The gap is the oldest
    postings, and nothing inside a 7-day freshness window sits behind it.
    """
    assert should_send(
        new_jobs=[_job()], changed_jobs=[], run_complete=False,
        stop_reasons={STOP_PAGE_CEILING},
    ) is True


def test_the_two_self_imposed_ceilings_together_still_send():
    assert should_send(
        new_jobs=[_job()], changed_jobs=[], run_complete=False,
        stop_reasons={STOP_BUDGET, STOP_PAGE_CEILING},
    ) is True


def test_a_provider_contradicting_its_own_total_still_silences_the_digest():
    """Deliberately *not* tolerated, unlike the two ceilings above.

    ``short_of_reported_total`` is the provider saying it has 679 postings and
    then serving 140. Nothing tells us which 539 are missing or whether they
    are old, so the hole has unknown shape - the same reason a failed page is
    fatal here.
    """
    assert should_send(
        new_jobs=[_job()], changed_jobs=[], run_complete=False,
        stop_reasons={STOP_SHORT_OF_TOTAL},
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


# --- more_results_available -------------------------------------------------

def test_a_teaser_page_truncation_still_sends_the_digest():
    """``more_results_available`` must not mean permanent silence.

    A handful of careers pages are teaser lists on every run and always will
    be, so treating them as fatal would suppress every digest forever - the
    same trap ``page_ceiling`` fell into before it was added to the rule.

    It is safe for a different reason than the ceilings: the gap is not
    newest-first, but it *is* stable, so the rows the tier does see are a
    consistent set and a new posting among them is genuinely new. The failure
    mode is a miss, never a false alarm.
    """
    from ats.base import STOP_MORE_AVAILABLE

    assert should_send(
        new_jobs=[{"job_id": "a"}],
        changed_jobs=[],
        run_complete=False,
        stop_reasons={STOP_MORE_AVAILABLE},
    ) is True


def test_a_teaser_truncation_beside_a_failed_page_still_suppresses():
    """One describable reason cannot launder an undescribable one."""
    from ats.base import STOP_MORE_AVAILABLE, STOP_PAGE_FAILED

    assert should_send(
        new_jobs=[{"job_id": "a"}],
        changed_jobs=[],
        run_complete=False,
        stop_reasons={STOP_MORE_AVAILABLE, STOP_PAGE_FAILED},
    ) is False


# --- scaling the rule to the size of the run --------------------------------

def _untrustworthy(count: int | None):
    """One run, ``count`` companies truncated in a way we cannot describe."""
    from ats.base import STOP_BUDGET, STOP_SHORT_OF_TOTAL

    return should_send(
        new_jobs=[{"job_id": "a"}],
        changed_jobs=[],
        run_complete=False,
        stop_reasons={STOP_BUDGET, STOP_SHORT_OF_TOTAL},
        untrustworthy_companies=count,
    )


def test_one_company_short_of_its_total_does_not_silence_the_run():
    """TEKsystems served 122 of the 136 it reported - 14 jobs, one employer.

    That alone used to suppress every alert for a 180-company workbook.
    """
    assert _untrustworthy(1) is True


def test_the_limit_itself_still_sends():
    from notify import UNTRUSTWORTHY_COMPANY_LIMIT
    assert _untrustworthy(UNTRUSTWORTHY_COMPANY_LIMIT) is True


def test_beyond_the_limit_is_systemic_and_suppresses():
    from notify import UNTRUSTWORTHY_COMPANY_LIMIT
    assert _untrustworthy(UNTRUSTWORTHY_COMPANY_LIMIT + 1) is False


def test_many_companies_short_still_suppresses():
    assert _untrustworthy(30) is False


def test_an_uncounted_run_keeps_the_cautious_behaviour():
    """A caller that has not been taught to count stays strict."""
    assert _untrustworthy(None) is False


def test_the_count_is_irrelevant_when_every_reason_is_describable():
    """Ceilings we set ourselves never suppress, however many companies hit them."""
    from ats.base import STOP_BUDGET, STOP_MORE_AVAILABLE

    assert should_send(
        new_jobs=[{"job_id": "a"}], changed_jobs=[], run_complete=False,
        stop_reasons={STOP_BUDGET, STOP_MORE_AVAILABLE},
        untrustworthy_companies=99,
    ) is True


def test_a_complete_run_ignores_the_count_entirely():
    assert should_send(
        new_jobs=[{"job_id": "a"}], changed_jobs=[], run_complete=True,
        stop_reasons=set(), untrustworthy_companies=99,
    ) is True


def test_nothing_to_announce_still_sends_nothing():
    """The count must never manufacture a digest out of an empty run."""
    assert _untrustworthy(0) is True  # sanity: reasons are describable-only path
    assert should_send(
        new_jobs=[], changed_jobs=[], run_complete=True,
        stop_reasons=set(), untrustworthy_companies=0,
    ) is False


def test_the_describable_set_has_one_definition():
    """notify and pipeline must not keep separate copies of it."""
    import notify as notify_module
    from ats.base import DESCRIBABLE_STOP_REASONS

    source = (notify_module.__file__ or "")
    assert source, "notify must be importable from a file"
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "DESCRIBABLE_STOP_REASONS" in text
    assert "describable = {STOP_BUDGET" not in text, "a second copy has crept back"
    assert "short_of_reported_total" not in DESCRIBABLE_STOP_REASONS
