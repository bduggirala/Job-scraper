"""Coverage gaps between the configured filters and the stated targets.

Three separate problems, all found by running the target lists in the brief
through the matchers rather than reading the patterns:

1. Ten ordinary DFW-metro cities were absent from ``config/settings.yaml``, so
   a Data Engineer role in Arlington or Carrollton was dropped silently.
2. ``_has_us_signal`` ends with "...or the text contains a remote token", and
   every remote job's text does. That clause therefore made the function
   return True for *every* remote posting, disabling the positive-evidence
   check its own docstring describes as the fix for an Accenture posting whose
   Brazilian locations named none of the blocked tokens.
3. "Software Engineer, Data Products" matched, because the segment splitter
   isolates "Data Products" and ``\\bdata\\b`` accepts it.
"""

import pytest

from filters import (
    REMOTE_NON_US,
    REMOTE_RESTRICTED,
    REMOTE_US,
    WORKPLACE_HYBRID,
    WORKPLACE_ONSITE,
    LocationMatcher,
    RoleMatcher,
    classify_remote_scope,
)
from settings import load_settings


@pytest.fixture(scope="module")
def cfg():
    return load_settings()


# --- DFW coverage ----------------------------------------------------------

DFW_CITIES = [
    # already configured
    "Dallas", "Fort Worth", "Irving", "Plano", "Richardson", "Frisco",
    "Addison", "Coppell", "Farmers Branch", "Westlake", "Grapevine",
    "Las Colinas",
    # ordinary DFW-metro cities that were missing
    "Arlington", "Carrollton", "Lewisville", "Denton", "McKinney", "Allen",
    "Garland", "Southlake", "Flower Mound", "Grand Prairie", "Mesquite",
    "Rockwall", "The Colony", "Euless", "Bedford", "Hurst", "Mansfield",
    "Cedar Hill", "DeSoto", "Duncanville", "Rowlett", "Wylie", "Little Elm",
    "Prosper", "Celina", "Roanoke", "Keller", "North Richland Hills",
]


@pytest.mark.parametrize("city", DFW_CITIES)
def test_a_dfw_city_matches(city, cfg):
    matched, reason = LocationMatcher(cfg).matches({"location": f"{city}, TX"})
    assert matched and reason == "dfw", f"{city}, TX was not treated as DFW"


@pytest.mark.parametrize("location", [
    "Arlington, VA",          # the other Arlington, and a common one
    "Richardson, UT",
    "Frisco, CO",
    "Westlake Village, CA",
    "Allen, MI",
    "Denton, MD",
])
def test_a_same_named_city_in_another_state_does_not_match(location, cfg):
    matched, _ = LocationMatcher(cfg).matches({"location": location})
    assert not matched, f"{location} matched as DFW"


@pytest.mark.parametrize("location", [
    "Arlington, Virginia, USA",
    "Richardson, Utah",
    "Frisco, Colorado",
    "Westlake, Ohio",
    "Allen, Michigan",
    "Roanoke, Virginia",
    "Bedford, Massachusetts",
    "Mansfield, Ohio",
])
def test_a_state_written_out_in_full_also_disqualifies(location, cfg):
    """The abbreviation branch of ``_conflicting_state`` is case-insensitive
    and the full-name branch was not, so every state spelled out in full went
    unnoticed - the guard only ever worked on "City, ST".

    Found by running the expanded city list against a real 115,368-row harvest:
    eight Amazon postings in "Arlington, Virginia, USA" came back as DFW
    matches. Every existing test used the abbreviation form, so nothing caught
    it.
    """
    matched, _ = LocationMatcher(cfg).matches({"location": location})
    assert not matched, f"{location} matched as DFW"


@pytest.mark.parametrize("location", [
    "Dallas, Texas", "Plano, Texas", "McKinney, Texas, USA", "Irving, TEXAS",
])
def test_a_texas_city_written_out_in_full_still_matches(location, cfg):
    matched, reason = LocationMatcher(cfg).matches({"location": location})
    assert matched and reason == "dfw", f"{location} was rejected"


# --- remote scope ----------------------------------------------------------

@pytest.mark.parametrize("location,expected", [
    ("Remote", REMOTE_US),
    ("Remote, USA", REMOTE_US),
    ("Remote - United States", REMOTE_US),
    ("Anywhere in the US", REMOTE_US),
    ("Work from home", REMOTE_US),
    ("Remote - TX", REMOTE_US),
    ("Remote - New York", REMOTE_RESTRICTED),
    ("Remote (NY, NJ, CT)", REMOTE_RESTRICTED),
    ("Remote (India)", REMOTE_NON_US),
    ("Remote - Bengaluru", REMOTE_NON_US),
    ("Hybrid - Dallas, TX", WORKPLACE_HYBRID),
    ("Dallas, TX", WORKPLACE_ONSITE),
    ("", WORKPLACE_ONSITE),
])
def test_remote_scope_classification(location, expected):
    assert classify_remote_scope({"location": location, "title": "Data Engineer"}) == expected


@pytest.mark.parametrize("location", [
    "Remote (Pernambuco, Recife)",
    "Remote - Porto Alegre",
    "Remote, Maranhão",
    "Remote - Kraków",
    "Remote (Cebu)",
])
def test_a_remote_role_in_an_unlisted_foreign_city_is_not_us(location):
    """The blocklist can never be exhaustive; positive evidence is the guard.

    None of these name a blocked country token, so before the fix each one
    classified as remote_us and reached the output as a DFW/remote match.
    """
    assert classify_remote_scope({"location": location, "title": "Data Engineer"}) \
        == REMOTE_NON_US, f"{location} passed as US-eligible"


def test_a_remote_us_role_still_reaches_the_output(cfg):
    """The guard must not cost the roles it exists to protect."""
    matched, reason = LocationMatcher(cfg).matches(
        {"location": "Remote - United States", "title": "Data Engineer"})
    assert matched and reason == "remote_us"


# --- role matching ---------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Data Engineer", "Senior Data Engineer", "Data Engineer II",
    "Data Engineer III", "Data Platform Engineer", "Cloud Data Engineer",
    "Snowflake Engineer", "Databricks Engineer", "Big Data Engineer",
    "ETL Developer", "ETL Engineer", "Analytics Engineer",
    "Lead Data Engineer", "Principal Data Engineer", "Staff Data Engineer",
])
def test_a_target_role_matches(title, cfg):
    assert RoleMatcher(cfg).matches(title), f"{title!r} was filtered out"


@pytest.mark.parametrize("title", [
    "Data Analyst", "Data Scientist", "Database Administrator",
    "Software Engineer, Data Products", "Engineering Manager",
    "Business Intelligence Analyst", "Data Engineering Intern",
    "Data Entry Clerk", "Data Center Technician", "Data Architect",
    "Director of Data Engineering", "VP, Data Platform",
    "Senior Manager, Data Science - US Card",
])
def test_a_non_target_role_is_rejected(title, cfg):
    assert not RoleMatcher(cfg).matches(title), f"{title!r} was accepted"
