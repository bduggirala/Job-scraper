"""Regression coverage for filter_companies_by_name.

Real bug: --test-company "Experis (ManpowerGroup)" (and "Robert Half (incl.
Robert Half Technology)", "Somnigroup International (Tempur Sealy)") raised
"No company in the workbook matches" even though those exact rows existed.
pandas' str.contains() treats its argument as a regex by default, so the
literal parentheses in these company names were parsed as a (zero-width)
capture group instead of literal characters - the needle only matched a
company name with the parens stripped out, which never exists.
"""

import pandas as pd

from pipeline import filter_companies_by_name


def _companies(*names):
    return pd.DataFrame({"company": list(names)})


def test_matches_a_name_containing_parentheses():
    df = _companies("Experis (ManpowerGroup)", "Verizon")
    result = filter_companies_by_name(df, "Experis (ManpowerGroup)")
    assert list(result["company"]) == ["Experis (ManpowerGroup)"]


def test_matches_a_name_containing_a_period():
    df = _companies("Robert Half (incl. Robert Half Technology)", "Kforce")
    result = filter_companies_by_name(df, "Robert Half (incl. Robert Half Technology)")
    assert list(result["company"]) == ["Robert Half (incl. Robert Half Technology)"]


def test_partial_substring_still_matches():
    df = _companies("Somnigroup International (Tempur Sealy)", "AT&T")
    result = filter_companies_by_name(df, "tempur")
    assert list(result["company"]) == ["Somnigroup International (Tempur Sealy)"]


def test_no_match_returns_empty():
    df = _companies("Verizon", "AT&T")
    result = filter_companies_by_name(df, "Nonexistent Co")
    assert result.empty
