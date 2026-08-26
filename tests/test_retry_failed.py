"""Re-running only the companies a previous run could not finish.

A full run takes the better part of an hour, and the handful of companies that
time out or hit a transient 503 are usually fine on a second attempt. Without a
way to name them, the only options were re-running all 180 or copying company
names out of a log by hand - so in practice the failures were left alone until
the next scheduled run.

``last_run.json`` already records every company and its status, which is exactly
the list. What counts as retryable is a deliberate choice:

* ``failed`` - a timeout or an error. The reason to retry.
* ``partial`` - real rows, incomplete coverage, and removal sync was skipped.
  A clean retry is what restores it.
* ``blocked`` - excluded. The site issued a challenge or an explicit denial;
  asking again is not a fix, and repeating it is exactly the behaviour the
  refusal was about.
* ``no_jobs`` / ``success`` - nothing to redo.
"""

import json

import pytest

from pipeline import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_NO_JOBS,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    retryable_companies,
)


def _report(tmp_path, rows, name="last_run.json"):
    path = tmp_path / name
    path.write_text(json.dumps({
        "run_id": "R1",
        "companies": [{"company": c, "status": s} for c, s in rows],
    }), encoding="utf-8")
    return path


def test_failed_and_partial_companies_are_retryable(tmp_path):
    path = _report(tmp_path, [
        ("Acme", STATUS_FAILED),
        ("Beta", STATUS_PARTIAL),
        ("Gamma", STATUS_SUCCESS),
        ("Delta", STATUS_NO_JOBS),
    ])
    assert retryable_companies(path) == ["Acme", "Beta"]


def test_a_blocked_company_is_not_retried(tmp_path):
    """Asking a site again after it refused is not a fix."""
    path = _report(tmp_path, [("Acme", STATUS_BLOCKED), ("Beta", STATUS_FAILED)])
    assert retryable_companies(path) == ["Beta"]


def test_a_clean_run_leaves_nothing_to_retry(tmp_path):
    path = _report(tmp_path, [("Acme", STATUS_SUCCESS), ("Beta", STATUS_NO_JOBS)])
    assert retryable_companies(path) == []


def test_a_missing_report_is_a_clear_error_not_a_silent_empty_list(tmp_path):
    """An empty list would read as "nothing failed", which is the opposite of
    "there is no report to read"."""
    with pytest.raises(FileNotFoundError):
        retryable_companies(tmp_path / "never_written.json")


def test_a_corrupt_report_is_reported_as_such(tmp_path):
    path = tmp_path / "last_run.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        retryable_companies(path)


def test_the_cli_exposes_the_retry(tmp_path):
    from main import build_parser
    args = build_parser().parse_args(["--retry-failed"])
    assert args.retry_failed is True


def test_the_retry_names_every_company_exactly_once(tmp_path):
    """A company can appear under two statuses in a hand-edited report; the
    retry must still visit it once rather than scraping it twice."""
    path = _report(tmp_path, [("Acme", STATUS_FAILED), ("Acme", STATUS_PARTIAL)])
    assert retryable_companies(path) == ["Acme"]
