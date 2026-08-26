"""The seven-day freshness window, at its edges.

``hours_old: 168`` is the whole freshness policy, and the edge is where a
window goes wrong: an exclusive comparison drops a job posted exactly seven
days ago, and a naive-vs-aware datetime comparison raises rather than filters.
Both are silent misses of exactly the kind this pipeline exists to avoid, so
the boundary is pinned here rather than left to the general filter tests.

The window is **inclusive** at the far edge. Where a rounding choice can go
either way, it goes toward keeping the job: a posting shown to a human who then
decides is recoverable; one filtered away is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from filters import (
    DATE_UNAVAILABLE,
    OUTSIDE_WINDOW,
    WITHIN_WINDOW,
    apply_filters,
    classify_date,
)
from settings import load_settings

#: A fixed "now" so these assertions never depend on when they run.
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
WINDOW_HOURS = 168  # seven days


def _posted(delta: timedelta) -> dict:
    return {"date_posted": (NOW - delta).isoformat()}


# --- the configured window -------------------------------------------------

def test_the_shipped_config_really_is_seven_days():
    """The requirement is 7 days; the config expresses it in hours."""
    assert int(load_settings().get("hours_old")) == WINDOW_HOURS == 7 * 24


# --- the boundary ----------------------------------------------------------

def test_exactly_seven_days_old_is_kept():
    """The far edge is inclusive: keep it rather than lose it."""
    assert classify_date(_posted(timedelta(days=7)), WINDOW_HOURS, now=NOW) == WITHIN_WINDOW


def test_a_second_newer_than_the_edge_is_kept():
    record = _posted(timedelta(days=7) - timedelta(seconds=1))
    assert classify_date(record, WINDOW_HOURS, now=NOW) == WITHIN_WINDOW


def test_a_second_older_than_the_edge_is_dropped():
    record = _posted(timedelta(days=7) + timedelta(seconds=1))
    assert classify_date(record, WINDOW_HOURS, now=NOW) == OUTSIDE_WINDOW


@pytest.mark.parametrize("delta,expected", [
    (timedelta(0), WITHIN_WINDOW),                        # posted this instant
    (timedelta(hours=1), WITHIN_WINDOW),
    (timedelta(days=1), WITHIN_WINDOW),
    (timedelta(days=6, hours=23, minutes=59), WITHIN_WINDOW),
    (timedelta(hours=168), WITHIN_WINDOW),                # the edge itself
    (timedelta(hours=168, minutes=1), OUTSIDE_WINDOW),
    (timedelta(days=8), OUTSIDE_WINDOW),
    (timedelta(days=30), OUTSIDE_WINDOW),
])
def test_the_window_walk(delta, expected):
    assert classify_date(_posted(delta), WINDOW_HOURS, now=NOW) == expected


def test_a_future_posting_date_is_kept():
    """Some tenants stamp a go-live date. Never older than the cutoff."""
    record = {"date_posted": (NOW + timedelta(days=2)).isoformat()}
    assert classify_date(record, WINDOW_HOURS, now=NOW) == WITHIN_WINDOW


# --- timezone normalization ------------------------------------------------

def test_a_naive_timestamp_is_read_as_utc_not_crashed_on():
    """ATS feeds emit naive stamps constantly; comparing them must not raise."""
    naive = (NOW - timedelta(days=3)).replace(tzinfo=None).isoformat()
    assert classify_date({"date_posted": naive}, WINDOW_HOURS, now=NOW) == WITHIN_WINDOW


def test_the_same_instant_in_two_zones_classifies_the_same():
    """Offsets must be honoured, not stripped."""
    instant = NOW - timedelta(days=3)
    as_utc = instant.isoformat()
    as_tokyo = instant.astimezone(timezone(timedelta(hours=9))).isoformat()
    assert classify_date({"date_posted": as_utc}, WINDOW_HOURS, now=NOW) == \
           classify_date({"date_posted": as_tokyo}, WINDOW_HOURS, now=NOW) == WITHIN_WINDOW


def test_an_offset_can_decide_the_boundary():
    """The offset is honoured, not stripped - and at the edge it decides.

    The same wall-clock reading sits on opposite sides of the cutoff depending
    on its zone: 12:00 on the seventh day is 03:00 UTC in +09:00 - nine hours
    *past* the cutoff, because a positive offset means the UTC instant is
    earlier - and 21:00 UTC in -09:00, nine hours inside it. Stripping the
    offset would classify both as the same edge case and quietly lose the first.
    """
    wall_clock = (NOW - timedelta(days=7)).replace(tzinfo=None)
    ahead = wall_clock.replace(tzinfo=timezone(timedelta(hours=9)))
    behind = wall_clock.replace(tzinfo=timezone(timedelta(hours=-9)))

    assert classify_date({"date_posted": ahead.isoformat()}, WINDOW_HOURS, now=NOW) == OUTSIDE_WINDOW
    assert classify_date({"date_posted": behind.isoformat()}, WINDOW_HOURS, now=NOW) == WITHIN_WINDOW


# --- relative phrasing, which is how Workday states it ---------------------

@pytest.mark.parametrize("phrase,expected", [
    ("Posted Today", WITHIN_WINDOW),
    ("Just posted", WITHIN_WINDOW),
    ("Posted Yesterday", WITHIN_WINDOW),
    ("Posted 6 Days Ago", WITHIN_WINDOW),
    ("Posted 7 Days Ago", WITHIN_WINDOW),
    ("Posted 8 Days Ago", OUTSIDE_WINDOW),
    ("Posted 1 Week Ago", WITHIN_WINDOW),
    ("Posted 2 Weeks Ago", OUTSIDE_WINDOW),
    ("Posted 30+ Days Ago", OUTSIDE_WINDOW),
])
def test_relative_phrasing_lands_on_the_right_side(phrase, expected):
    assert classify_date({"date_posted": phrase}, WINDOW_HOURS, now=NOW) == expected


# --- unavailable dates -----------------------------------------------------

def test_an_absent_date_is_marked_not_invented():
    """Substituting "now" would make every undated job permanently fresh."""
    assert classify_date({"date_posted": None}, WINDOW_HOURS, now=NOW) == DATE_UNAVAILABLE
    assert classify_date({}, WINDOW_HOURS, now=NOW) == DATE_UNAVAILABLE
    assert classify_date({"date_posted": "   "}, WINDOW_HOURS, now=NOW) == DATE_UNAVAILABLE


def test_unparseable_text_is_unavailable_rather_than_guessed():
    assert classify_date({"date_posted": "sometime soon"}, WINDOW_HOURS, now=NOW) \
           == DATE_UNAVAILABLE


def test_first_seen_rescues_an_undated_job_only_inside_the_window():
    """Undated, but we watched it appear - that observation is a date."""
    fresh = (NOW - timedelta(days=2)).isoformat()
    stale = (NOW - timedelta(days=20)).isoformat()
    assert classify_date({}, WINDOW_HOURS, now=NOW, first_seen=fresh) == WITHIN_WINDOW
    assert classify_date({}, WINDOW_HOURS, now=NOW, first_seen=stale) == OUTSIDE_WINDOW


def test_an_official_date_always_beats_first_seen():
    """first_seen is a fallback, never an override - it is our date, not theirs."""
    long_ago = (NOW - timedelta(days=90)).isoformat()
    record = {"date_posted": long_ago}
    assert classify_date(record, WINDOW_HOURS, now=NOW, first_seen=NOW.isoformat()) \
           == OUTSIDE_WINDOW


# --- end to end through apply_filters --------------------------------------

def _row(title, location, delta):
    return {
        "job_id": f"id-{title}-{delta}",
        "title": title,
        "location": location,
        "date_posted": (NOW - delta).isoformat(),
        "company": "Acme",
        "job_url": f"https://acme.example/{title}-{delta}".replace(" ", "-"),
    }


def test_apply_filters_keeps_the_edge_and_drops_just_past_it():
    rows = [
        _row("Data Engineer", "Dallas, TX", timedelta(days=7)),
        _row("Senior Data Engineer", "Plano, TX", timedelta(hours=167)),
        _row("Analytics Engineer", "Frisco, TX", timedelta(days=8)),
    ]
    result = apply_filters(rows, now=NOW)
    kept = {r["title"] for r in result["jobs"]}

    assert kept == {"Data Engineer", "Senior Data Engineer"}
    assert result["counts"]["older_than_window"] == 1
    assert result["counts"]["within_window"] == 2


def test_an_undated_row_survives_and_says_so_in_the_output():
    """Kept by policy - and flagged, so a reader knows the date is unknown."""
    row = _row("ETL Engineer", "Irving, TX", timedelta(days=1))
    row["date_posted"] = None

    result = apply_filters([row], now=NOW)

    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["date_filter_status"] == DATE_UNAVAILABLE
    assert result["counts"]["date_unavailable"] == 1


# --- date provenance -------------------------------------------------------
#
# "within_window" answers "is this fresh?" but not "according to whom?". Two
# very different rows carry it: one where the employer stamped a date three
# days ago, and one with no date at all that *we* first saw three days ago. The
# second is not a posting date - a role listed for six months by a company we
# only started scraping last week reads as brand new - so the export says which
# it is. On the run that prompted this, 13 of 31 exported rows rested on
# first_seen and were labelled identically to the ones that did not.

from filters import (  # noqa: E402
    DATE_FROM_FIRST_SEEN,
    DATE_FROM_NOTHING,
    DATE_FROM_POSTING,
    classify_date_with_source,
)


def test_an_employer_date_is_marked_as_such():
    status, source = classify_date_with_source(_posted(timedelta(days=2)), WINDOW_HOURS, now=NOW)
    assert (status, source) == (WITHIN_WINDOW, DATE_FROM_POSTING)


def test_a_first_seen_rescue_is_marked_as_a_rescue():
    fresh = (NOW - timedelta(days=2)).isoformat()
    status, source = classify_date_with_source({}, WINDOW_HOURS, now=NOW, first_seen=fresh)
    assert (status, source) == (WITHIN_WINDOW, DATE_FROM_FIRST_SEEN)


def test_no_date_at_all_is_marked_as_nothing():
    status, source = classify_date_with_source({}, WINDOW_HOURS, now=NOW)
    assert (status, source) == (DATE_UNAVAILABLE, DATE_FROM_NOTHING)


def test_an_employer_date_still_wins_over_first_seen():
    status, source = classify_date_with_source(
        _posted(timedelta(days=90)), WINDOW_HOURS, now=NOW, first_seen=NOW.isoformat(),
    )
    assert (status, source) == (OUTSIDE_WINDOW, DATE_FROM_POSTING)


def test_classify_date_is_unchanged_by_the_addition():
    """The one-value function keeps its exact contract."""
    for record, seen in (
        (_posted(timedelta(days=2)), None),
        (_posted(timedelta(days=90)), None),
        ({}, (NOW - timedelta(days=2)).isoformat()),
        ({}, None),
    ):
        status, _ = classify_date_with_source(record, WINDOW_HOURS, now=NOW, first_seen=seen)
        assert classify_date(record, WINDOW_HOURS, now=NOW, first_seen=seen) == status


def test_the_export_carries_the_provenance():
    rows = [
        _row("Data Engineer", "Dallas, TX", timedelta(days=2)),
        _row("ETL Engineer", "Plano, TX", timedelta(days=1)),
    ]
    rows[1]["date_posted"] = None
    lookup = {rows[1]["job_id"]: (NOW - timedelta(days=1)).isoformat()}

    result = apply_filters(rows, now=NOW, first_seen_lookup=lookup)
    by_title = {r["title"]: r for r in result["jobs"]}

    assert by_title["Data Engineer"]["date_source"] == DATE_FROM_POSTING
    assert by_title["ETL Engineer"]["date_source"] == DATE_FROM_FIRST_SEEN
    assert by_title["ETL Engineer"]["date_filter_status"] == WITHIN_WINDOW, (
        "still kept - the point is that it says why"
    )


def test_date_source_is_an_exported_column():
    from pipeline import OUTPUT_FIELDS
    assert "date_source" in OUTPUT_FIELDS
