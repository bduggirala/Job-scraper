"""Fit scoring must explain itself.

The brief is explicit: "Do not return an unexplained single score." A bare 7.4
is unactionable - you cannot tell whether it means "matches your Spark work" or
"mentions SQL once". The signals that produced it, and the ones that were
looked for and missing, are the useful part.

Scoring depends on descriptions, and several collectors return none. A job with
no description must therefore be reported as *unscored*, not as a zero - those
mean opposite things to someone reading the output.
"""

import pytest

from fit import score_fit


def _job(description=None, title="Data Engineer"):
    return {"title": title, "description": description}


def test_matched_skills_are_named():
    result = score_fit(_job("Python, Spark and Snowflake on AWS."))

    assert set(result.matched) >= {"python", "spark", "snowflake", "aws"}
    assert result.score > 0


def test_skills_looked_for_but_absent_are_named():
    result = score_fit(_job("Python and SQL only."))

    assert "kafka" in result.missing
    assert "kafka" not in result.matched


def test_a_job_without_a_description_is_unscored_not_zero():
    """Several collectors return no description; that is not a bad match."""
    result = score_fit(_job(description=None))

    assert result.score is None
    assert result.explanation == "no description available"


def test_an_empty_description_is_also_unscored():
    assert score_fit(_job(description="   ")).score is None


def test_more_matching_skills_scores_higher():
    weak = score_fit(_job("Some SQL required."))
    strong = score_fit(_job("Python, PySpark, Kafka, Snowflake, Databricks, AWS, ETL."))

    assert strong.score > weak.score


def test_the_score_is_bounded():
    result = score_fit(_job("python " * 500 + "spark kafka snowflake aws sql etl"))
    assert 0 <= result.score <= 100


def test_a_skill_is_not_matched_inside_an_unrelated_word():
    """'R' and 'Go' style short tokens must not match everywhere."""
    result = score_fit(_job("Great opportunity in Argo Tunnel administration."))
    assert "go" not in result.matched


def test_the_explanation_lists_the_matches():
    result = score_fit(_job("Python and Airflow."))
    assert "python" in result.explanation.lower()


def test_the_title_contributes_when_the_description_mentions_nothing():
    """A Snowflake Engineer is a Snowflake match even with a thin description."""
    result = score_fit(_job(description="See posting.", title="Snowflake Engineer"))
    assert "snowflake" in result.matched
