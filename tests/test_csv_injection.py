"""Scraped text must not become a live formula when the CSV is opened.

Titles, locations and descriptions come verbatim from third-party pages, and
the output exists to be opened in a spreadsheet. A cell whose value begins with
=, +, - or @ is interpreted as a formula by Excel, LibreOffice and Sheets, so a
crafted job title would execute on open. Low likelihood, high blast radius, and
a four-line fix.
"""

import pandas as pd
import pytest

from pipeline import OUTPUT_FIELDS, write_outputs
from settings import load_settings


def _job(**kw):
    row = {f: None for f in OUTPUT_FIELDS}
    row.update({"company": "Acme", "title": "Data Engineer",
                "job_url": "https://x.test/1", "ats_provider": "workday",
                "scraping_method": "direct_api"})
    row.update(kw)
    return row


@pytest.mark.parametrize("hostile", [
    '=HYPERLINK("http://evil.test","Click")',
    '+1+1',
    '-2+3',
    '@SUM(A1:A9)',
    '=cmd|\' /c calc\'!A0',
])
def test_a_formula_leading_character_is_neutralised(tmp_path, monkeypatch, hostile):
    cfg = load_settings()
    monkeypatch.setattr(cfg, "resolve_path", lambda *a, **kw: tmp_path)

    write_outputs([_job(title=hostile)], [], cfg)

    value = pd.read_csv(tmp_path / "company_jobs.csv")["title"].iloc[0]

    assert not value.startswith(("=", "+", "-", "@")), (
        f"formula character survived into the CSV: {value!r}"
    )
    # Neutralised, not mangled: the original text is preserved behind the
    # apostrophe, which a spreadsheet consumes and does not display.
    assert value == f"'{hostile}"


def test_ordinary_titles_are_untouched(tmp_path, monkeypatch):
    cfg = load_settings()
    monkeypatch.setattr(cfg, "resolve_path", lambda *a, **kw: tmp_path)

    write_outputs([_job(title="Senior Data Engineer", location="Plano, TX")], [], cfg)

    frame = pd.read_csv(tmp_path / "company_jobs.csv")
    assert frame["title"].iloc[0] == "Senior Data Engineer"
    assert frame["location"].iloc[0] == "Plano, TX"


def test_a_negative_number_in_a_text_column_is_still_neutralised(tmp_path, monkeypatch):
    """'-Remote' is text, and text beginning with '-' is a formula to Excel."""
    cfg = load_settings()
    monkeypatch.setattr(cfg, "resolve_path", lambda *a, **kw: tmp_path)

    write_outputs([_job(location="-Remote, US")], [], cfg)

    value = pd.read_csv(tmp_path / "company_jobs.csv")["location"].iloc[0]
    assert not value.startswith("-")
