"""What the exported spreadsheet has to contain, and be safe to open.

The output carried ``is_new`` and nothing else about a job's state, so a run
could not tell a reader which rows had *moved* - the change detection existed
in the database and reached the email, but never the file most people actually
open. ``change_status`` closes that.

Removed jobs are deliberately absent: this file lists what the employer is
advertising now, and a row for a job that no longer exists would be a link to
a dead page. Removals are reported in the run summary instead.
"""

import pandas as pd
import pytest

from pipeline import OUTPUT_FIELDS, escape_formulas, write_outputs
from ats.router import CompanyResult, RoutePlan


REQUIRED = [
    "company", "title", "location", "date_posted", "job_url", "apply_url",
    "ats_provider",        # source provider
    "scraping_method",     # extraction method
    "remote_scope", "location_match_type", "date_filter_status",
    "first_seen", "is_new", "change_status",
    "fit_score", "fit_matched", "fit_explanation",
]


@pytest.mark.parametrize("column", REQUIRED)
def test_the_export_carries_the_column(column):
    assert column in OUTPUT_FIELDS, f"{column} is missing from the export"


def _job(**over):
    base = {f: None for f in OUTPUT_FIELDS}
    base.update({
        "company": "Acme", "title": "Data Engineer", "location": "Dallas, TX",
        "job_url": "https://acme.test/jobs/1", "ats_provider": "greenhouse",
        "scraping_method": "direct_api", "remote_scope": "onsite",
        "is_new": True, "change_status": "new",
    })
    base.update(over)
    return base


def _plan():
    return RoutePlan(company="Acme", url="https://acme.test", provider="greenhouse",
                     method="direct_api", source="ats_url")


def test_the_written_files_can_be_read_back(tmp_path, monkeypatch):
    from settings import load_settings
    cfg = load_settings()
    monkeypatch.setattr(cfg, "resolve_path",
                        lambda key, default=None: tmp_path if "directory" in key
                        else load_settings().resolve_path(key, default))

    paths = write_outputs([_job()], [CompanyResult("Acme", [], _plan(), True)], cfg)

    frame = pd.read_csv(paths["csv"])
    assert list(frame.columns) == OUTPUT_FIELDS
    assert len(frame) == 1
    assert "xlsx" in paths and paths["xlsx"].exists()
    pd.read_excel(paths["xlsx"])          # opens without error


def test_unicode_survives_the_round_trip(tmp_path, monkeypatch):
    from settings import load_settings
    cfg = load_settings()
    monkeypatch.setattr(cfg, "resolve_path", lambda key, default=None: tmp_path)

    job = _job(company="Children’s Health", location="Maranhão / Zürich",
               title="Données Engineer — Senior")
    paths = write_outputs([job], [], cfg)

    frame = pd.read_csv(paths["csv"], encoding="utf-8")
    assert frame.iloc[0]["company"] == "Children’s Health"
    assert frame.iloc[0]["location"] == "Maranhão / Zürich"
    assert "—" in frame.iloc[0]["title"]


@pytest.mark.parametrize("dangerous", [
    "=HYPERLINK(\"http://evil\",\"click\")",
    "+1+1",
    "-2+3",
    "@SUM(A1:A9)",
])
def test_a_formula_in_scraped_text_is_neutralised(dangerous):
    frame = pd.DataFrame([{"title": dangerous, "company": "Acme"}])
    escaped = escape_formulas(frame)
    assert escaped.iloc[0]["title"].startswith("'"), (
        f"{dangerous!r} would execute when the sheet is opened"
    )


def test_ordinary_text_is_left_alone():
    frame = pd.DataFrame([{"title": "Data Engineer", "location": "Dallas, TX"}])
    escaped = escape_formulas(frame)
    assert escaped.iloc[0]["title"] == "Data Engineer"


def test_change_status_distinguishes_the_three_live_states():
    """new / changed / unchanged all have to be visible in the file."""
    from pipeline import assign_change_status

    jobs = [
        {"job_id": "a", "is_new": True},
        {"job_id": "b", "is_new": False},
        {"job_id": "c", "is_new": False},
    ]
    assign_change_status(jobs, changed_ids={"b"})

    assert [j["change_status"] for j in jobs] == ["new", "changed", "unchanged"]


def test_a_new_job_that_also_changed_reads_as_new():
    """"New" is the more useful label; it cannot also be a change to report."""
    from pipeline import assign_change_status

    jobs = [{"job_id": "a", "is_new": True}]
    assign_change_status(jobs, changed_ids={"a"})
    assert jobs[0]["change_status"] == "new"
