"""Remote is not one thing, and treating it as one lets the wrong jobs through.

``is_remote_us`` returned a bool, and its US check accepted *any* US state
name as positive evidence. So "Remote - must reside in New York" satisfied both
the remote token and the US signal, and reached the output as a DFW/remote
match despite being neither.

The brief asks to distinguish fully-remote-US from hybrid, from
location-restricted remote, and from non-US remote. That needs a scope, not a
boolean.
"""

import pytest

from filters import (
    REMOTE_NON_US,
    REMOTE_RESTRICTED,
    REMOTE_US,
    WORKPLACE_HYBRID,
    WORKPLACE_ONSITE,
    LocationMatcher,
    classify_remote_scope,
)


def _rec(location=None, title="Data Engineer", remote=None):
    return {"location": location, "title": title, "remote": remote}


@pytest.mark.parametrize("location", [
    "Remote, United States",
    "Remote - US",
    "Remote (Anywhere in the US)",
    "US Remote",
])
def test_fully_remote_us_is_recognised(location):
    assert classify_remote_scope(_rec(location)) == REMOTE_US


@pytest.mark.parametrize("location", [
    "Remote - must reside in New York",
    "Remote (California only)",
    "Remote - Washington State residents",
])
def test_remote_restricted_to_another_state_is_not_us_remote(location):
    """The live false positive: a state name was read as US eligibility."""
    assert classify_remote_scope(_rec(location)) == REMOTE_RESTRICTED


def test_remote_restricted_to_texas_still_counts_as_reachable():
    """A Texas restriction is not a restriction for a DFW search."""
    assert classify_remote_scope(_rec("Remote - Texas only")) == REMOTE_US


@pytest.mark.parametrize("location", [
    "Remote, India",
    "Remote - EMEA",
    "Remote (Bengaluru)",
])
def test_non_us_remote_is_recognised(location):
    assert classify_remote_scope(_rec(location)) == REMOTE_NON_US


@pytest.mark.parametrize("location", [
    "Hybrid - Dallas, TX",
    "Plano, TX (Hybrid)",
    "Dallas, TX - 3 days onsite",
])
def test_hybrid_is_not_remote(location):
    assert classify_remote_scope(_rec(location)) == WORKPLACE_HYBRID


def test_an_ordinary_office_location_is_onsite():
    assert classify_remote_scope(_rec("Plano, TX")) == WORKPLACE_ONSITE


# --- how the matcher uses it ----------------------------------------------

def test_a_state_restricted_remote_role_no_longer_matches():
    matched, reason = LocationMatcher().matches(_rec("Remote - must reside in New York"))
    assert matched is False, f"still matched as {reason}"


def test_a_dfw_hybrid_role_still_matches_on_location():
    """Hybrid in Plano is reachable - it is only 'not remote', not 'not ours'."""
    matched, reason = LocationMatcher().matches(_rec("Plano, TX (Hybrid)"))
    assert matched is True
    assert reason == "dfw"


def test_a_genuinely_remote_us_role_still_matches():
    matched, reason = LocationMatcher().matches(_rec("Remote, United States"))
    assert matched is True
    assert reason == "remote_us"
