"""Tests for LocationMatcher (DFW-metro and remote-US matching).

No prior test coverage existed for this class. Includes a regression case
for a real bug found in a live full run: an Accenture posting listing only
Brazilian states/cities and explicit_remote=True was accepted as "remote_us"
because the non-US blocklist doesn't (and can't exhaustively) name every
non-US city - only the country name "brazil" was blocked, not "Recife" or
"Porto Alegre".
"""

import pytest

from filters import LocationMatcher


@pytest.fixture
def matcher():
    return LocationMatcher()


# --- DFW matching -----------------------------------------------------------

@pytest.mark.parametrize("location", [
    "Dallas, TX", "Plano, Texas", "Fort Worth, TX", "Irving, TX, United States",
    "Frisco, TX", "Las Colinas, Texas", "Dallas-Fort Worth, TX",
])
def test_matches_dfw_cities(matcher, location):
    assert matcher.is_dfw({"location": location}) is True


@pytest.mark.parametrize("location", [
    "Westlake Village, CA",  # same-named city, wrong state
    "Richardson, UT",
    "Frisco, CO",
    "Seattle, WA",
    None,
    "",
])
def test_rejects_same_named_city_in_another_state(matcher, location):
    assert matcher.is_dfw({"location": location}) is False


# --- Remote-US matching ------------------------------------------------------

def test_accepts_bare_remote_marker(matcher):
    assert matcher.is_remote_us({"location": "Remote", "remote": True}) is True


def test_accepts_remote_with_us_state_abbreviations(matcher):
    record = {"location": "Remote-CA | Remote-ID | Remote-NM", "remote": True}
    assert matcher.is_remote_us(record) is True


def test_accepts_united_states_location(matcher):
    record = {"location": "United States", "remote": True}
    assert matcher.is_remote_us(record) is True


def test_a_named_us_state_and_city_is_recognised_as_american(matcher):
    """Still US, but anchored to one state - so not remote-anywhere.

    This test originally asserted ``is_remote_us`` was True, back when that
    method only answered "is this American?". It now also has to answer "is
    this open anywhere in the US?", and a remote role listing San Diego,
    California is exactly the location-restricted case that used to reach a
    DFW search as a match. The US/non-US axis it was written to cover is
    asserted directly instead.
    """
    from filters import REMOTE_RESTRICTED, classify_remote_scope

    record = {"location": "San Diego, CALIFORNIA, us", "remote": True}
    assert classify_remote_scope(record) == REMOTE_RESTRICTED
    assert matcher.is_remote_us(record) is False


def test_rejects_foreign_cities_not_in_the_non_us_blocklist(matcher):
    """Regression: Accenture posting listing only Brazilian states/cities,
    remote=True, none of which match _NON_US_TOKENS (only "brazil" the
    country name is blocked, not its city names)."""
    record = {
        "title": "Consultor(a) SAP Data Migration",
        "location": (
            "Pernambuco - Recife | Maranhão - Porto Alegre | "
            "Campina Grande, Unifacisa Building 7 | Florianopolis, BeWiki | "
            "Rio de Janeiro, Rio Metropolitan Center"
        ),
        "remote": True,
    }
    assert matcher.is_remote_us(record) is False


def test_rejects_blocklisted_non_us_country(matcher):
    record = {"location": "London, UK", "remote": True}
    assert matcher.is_remote_us(record) is False


def test_rejects_when_not_marked_remote_and_no_remote_token(matcher):
    record = {"location": "Berlin, Germany", "remote": False}
    assert matcher.is_remote_us(record) is False


def test_accepts_blank_location_when_explicitly_remote(matcher):
    """No location detail at all carries no evidence either way - trusted
    as-is for this US-focused tool."""
    assert matcher.is_remote_us({"location": None, "remote": True}) is True


def test_include_remote_false_disables_remote_matching(matcher):
    matcher.include_remote = False
    assert matcher.is_remote_us({"location": "Remote", "remote": True}) is False


# --- matches() ---------------------------------------------------------------

def test_matches_prefers_dfw_reason_over_remote(matcher):
    matched, reason = matcher.matches({"location": "Dallas, TX", "remote": True})
    assert matched is True
    assert reason == "dfw"


def test_matches_returns_remote_reason(matcher):
    matched, reason = matcher.matches({"location": "Remote", "remote": True})
    assert matched is True
    assert reason == "remote_us"


def test_matches_returns_false_reason_none(matcher):
    matched, reason = matcher.matches({"location": "London, UK", "remote": True})
    assert matched is False
    assert reason is None
