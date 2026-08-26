"""Tests for RoleMatcher against the real config/settings.yaml patterns.

Loads the actual configured target_role_patterns / exclude_role_patterns
(not a hand-rolled substitute) so a future edit to settings.yaml that breaks
role matching fails here rather than only being noticed in a live run.
"""

import pytest

from filters import RoleMatcher


@pytest.fixture
def matcher():
    return RoleMatcher()


@pytest.mark.parametrize("title", [
    "Data Engineer",
    "Senior Data Engineer",
    "Data Engineer II",
    "Data Platform Engineer",
    "Snowflake Engineer",
    "Databricks Developer",
    "ETL Developer",
    "Analytics Engineer",
    "Data Analytics Engineer",
])
def test_matches_individual_contributor_data_roles(matcher, title):
    assert matcher.matches(title) is True


@pytest.mark.parametrize("title", [
    "Lead Data Engineer",
    "Senior Lead Data Engineer",
    "Principal Data Engineer",
    "Distinguished Data Engineer",
])
def test_matches_senior_individual_contributor_titles(matcher, title):
    """lead/principal are IC titles at most employers, not management.

    Excluding them cost real postings: a live Capital One run returned 44
    "Lead Data Engineer" / "Senior Lead Data Engineer" roles that the old
    '\b(lead|manager|principal)\b' pattern discarded outright - the most
    relevant senior openings in the whole tenant.
    """
    assert matcher.matches(title) is True


@pytest.mark.parametrize("title", [
    "Data Engineering Manager",
    "Manager, Data Platform",
    "Director, Data Engineering",
    "Head of Data Engineering",
    "VP, Data Platform",
])
def test_excludes_management_titles(matcher, title):
    """Management is excluded by shape - manager/director/head of/VP - rather
    than by seniority words, which caught individual contributors too."""
    assert matcher.matches(title) is False


@pytest.mark.parametrize("title", [
    "Data Analyst",
    "Business Data Analyst",
    "Sr. Analyst, Data Analysis",
    "Data Architect",
    "Business Intelligence Developer",
    "Data Governance Specialist",
    "Data Annotator",
])
def test_excludes_non_engineering_data_roles(matcher, title):
    """The false-positive classes '\bdata\b' pulls in that are not
    engineering. '\banalyst\b' is deliberately its own word so that
    "Data Analytics Engineer" and "Analytics Engineer" still match."""
    assert matcher.matches(title) is False


@pytest.mark.parametrize("title", [
    "Database Administrator",
    "Database Engineer",
])
def test_does_not_match_database_as_a_substring(matcher, title):
    """'\\bdata\\b' must not match inside "database" - no word boundary there."""
    assert matcher.matches(title) is False


@pytest.mark.parametrize("title", [
    "Data Scientist",
    "Machine Learning Engineer",
    "ML Engineer",
    "Data Engineer Intern",
    "Data Engineering Co-op",
])
def test_still_excludes_previously_excluded_roles(matcher, title):
    assert matcher.matches(title) is False


@pytest.mark.parametrize("title", [
    "Specialist, Data Center Operations",
    "HVAC Technician Data Center",
    "Director - Data Centers, Client Sourcing/Procurement",
    "Sr. Sales Executive: Data Centers",
    "Customer Success Specialist - Data Entry",
])
def test_excludes_data_center_and_data_entry_false_positives(matcher, title):
    """'\\bdata\\b' pulls these in, but they're facilities/clerical work,
    not data engineering - confirmed live in a 65k-job real full run."""
    assert matcher.matches(title) is False


def test_no_title_does_not_match(matcher):
    assert matcher.matches(None) is False
    assert matcher.matches("") is False


def test_unrelated_title_does_not_match(matcher):
    assert matcher.matches("Warehouse Associate") is False


def test_readme_documented_cross_segment_example_still_matches(matcher):
    """README: 'Software Engineer, Data Engineering' matches via its second
    segment even though 'Software Engineer' alone would not - must still
    hold after moving the exclude check to the whole title."""
    assert matcher.matches("Software Engineer, Data Engineering") is True
