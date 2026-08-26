"""A remote token that is really part of a street address.

Found in the exported spreadsheet of a full workbook run: one of 23 matched
rows was an Accenture posting whose ``remote_scope`` read ``remote_non_us``
while its ``location_match_type`` read ``dfw``. Both cannot be true, and the
location says plainly which one is wrong::

    Nashville, 4101 Charlotte Ave., Corp
    | Dallas, 5205 N OConnor Las Colinas, Corp
    | Atlanta, 3565 Piedmont Rd NE., ACN Ops
    | Columbus, 400 W. Nationwide Blvd, Corp

Four US cities, one of them Dallas. What flipped it was **"400 W. Nationwide
Blvd"** - Nationwide Boulevard in Columbus, Ohio - matching the remote token
``nationwide``. The record then went down the remote branch, where the leftover
geography ("nashville ... dallas ... atlanta ... columbus") names no state and
no country, so the positive-US-evidence check failed and it landed on
``remote_non_us``.

The row still reached the output, because the DFW city match is a separate
axis - so this cost no recall. It put a plainly wrong label on a row a person
reads, which is its own kind of wrong.

A remote token directly followed by a street-type suffix is part of an address.
Nobody writes "Remote Boulevard" to mean the job is remote.
"""

import pytest

from filters import (
    REMOTE_NON_US,
    REMOTE_US,
    WORKPLACE_ONSITE,
    classify_remote_scope,
)

ACCENTURE = (
    "Nashville, 4101 Charlotte Ave., Corp "
    "| Dallas, 5205 N OConnor Las Colinas, Corp "
    "| Atlanta, 3565 Piedmont Rd NE., ACN Ops "
    "| Columbus, 400 W. Nationwide Blvd, Corp"
)


def test_the_accenture_row_is_not_a_non_us_remote_job():
    scope = classify_remote_scope(
        {"location": ACCENTURE, "title": "Epic Certified Clinical Data Model Consultant"}
    )
    assert scope != REMOTE_NON_US, (
        "a four-city US address list was labelled remote_non_us"
    )
    assert scope == WORKPLACE_ONSITE


@pytest.mark.parametrize("location", [
    "400 W. Nationwide Blvd, Columbus",
    "1 Nationwide Plaza, Columbus, OH",
    "200 Remote Road, Anytown",
    "17 Virtual Street, Springfield",
])
def test_a_street_named_after_a_remote_word_is_still_an_address(location):
    assert classify_remote_scope({"location": location, "title": "Data Engineer"}) \
        == WORKPLACE_ONSITE, f"{location!r} was read as a remote posting"


@pytest.mark.parametrize("location", [
    "Remote - Nationwide",
    "Nationwide (US)",
    "Remote",
    "Remote, USA",
    "Virtual - United States",
])
def test_a_genuine_remote_posting_is_unaffected(location):
    assert classify_remote_scope({"location": location, "title": "Data Engineer"}) \
        == REMOTE_US, f"{location!r} stopped counting as remote"


def test_an_address_does_not_suppress_a_separate_remote_token():
    """One token being an address must not disqualify another that is real."""
    location = "Remote - US | 400 W. Nationwide Blvd, Columbus"
    assert classify_remote_scope({"location": location, "title": "Data Engineer"}) \
        != WORKPLACE_ONSITE
