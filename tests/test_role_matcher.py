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
    "Data Analyst",
    "Data Architect",
    "Business Data Analyst",
    "Data Platform Engineer",
    "Snowflake Engineer",
    "Databricks Developer",
    "ETL Developer",
    "Analytics Engineer",
])
def test_matches_individual_contributor_data_roles(matcher, title):
    assert matcher.matches(title) is True


@pytest.mark.parametrize("title", [
    "Lead Data Engineer",
    "Data Engineering Manager",
    "Principal Data Engineer",
    "Data Engineer, Team Lead",
    "Manager, Data Platform",
])
def test_excludes_seniority_and_management_titles(matcher, title):
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
