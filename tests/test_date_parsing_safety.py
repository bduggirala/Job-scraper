"""Date parsing must not invent a date out of DOM noise.

Two things combined badly. The browser extractor looked for a posting date in
any element whose class matched ``/(date|posted|time|age)/i`` - and ``age``
matches ``page``, ``manager``, ``message``, ``package``. Whatever text those
elements held was then handed to ``dateutil`` with ``fuzzy=True``, which pulls
a date out of almost anything containing a number.

The failure direction is the bad one: a bogus *old* date classifies a fresh
posting as ``older_than_window`` and drops it. A job silently lost is much
worse than a job with a missing date, which is kept and flagged.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from normalize import parse_date

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", [
    "Building 7",
    "Suite 200",
    "Req 12345",
    "Team 4",
    "$120,000 - $150,000",
    "5 openings",
    "Full-time",
    "Level 3",
])
def test_arbitrary_page_text_does_not_become_a_date(text):
    """None of these are dates, and treating them as one loses the job."""
    assert parse_date(text, reference=NOW) is None


@pytest.mark.parametrize("text", [
    "2026-08-20",
    "August 20, 2026",
    "Posted 3 Days Ago",
    "Posted Today",
    "Yesterday",
    "20 Aug 2026",
])
def test_real_dates_still_parse(text):
    assert parse_date(text, reference=NOW) is not None


def test_an_implausibly_old_date_is_rejected():
    """A job posted in 1998 is a parsing artefact, not a stale posting."""
    assert parse_date("1998-01-01", reference=NOW) is None


def test_a_date_just_inside_the_plausible_window_is_kept():
    recent = (NOW - timedelta(days=200)).strftime("%Y-%m-%d")
    assert parse_date(recent, reference=NOW) is not None


def test_a_far_future_date_is_still_rejected():
    assert parse_date("2030-01-01", reference=NOW) is None


# --- the DOM-side half -----------------------------------------------------

def test_the_dom_date_selector_does_not_match_unrelated_class_names():
    """'age' matched page/manager/message/package - four very common classes."""
    from browser.playwright_scraper import _EXTRACT_JS

    match = re.search(r"const dateRe = /\(([^)]*)\)/i", _EXTRACT_JS)
    assert match, "date regex not found in the extraction script"
    pattern = re.compile(match.group(1), re.I)

    for class_name in ("page-header", "job-manager", "message-bar",
                       "package-info", "pagination"):
        assert not pattern.search(class_name), (
            f"date selector still matches the unrelated class {class_name!r}"
        )


def test_the_dom_date_selector_still_matches_real_date_classes():
    from browser.playwright_scraper import _EXTRACT_JS

    match = re.search(r"const dateRe = /\(([^)]*)\)/i", _EXTRACT_JS)
    pattern = re.compile(match.group(1), re.I)

    for class_name in ("posted-date", "job-date", "datePosted", "publish-date"):
        assert pattern.search(class_name), f"stopped matching {class_name!r}"
