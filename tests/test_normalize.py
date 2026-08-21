from datetime import datetime, timedelta, timezone

from normalize import build_record, parse_date

REF = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_date_handles_iso():
    assert parse_date("2026-08-18T00:00:00+00:00", reference=REF).day == 18


def test_parse_date_handles_relative_days():
    assert parse_date("Posted 3 Days Ago", reference=REF) == REF - timedelta(days=3)


def test_parse_date_handles_today():
    assert parse_date("Posted Today", reference=REF) == REF


def test_parse_date_returns_none_for_garbage():
    assert parse_date("see description", reference=REF) is None


def test_parse_date_rejects_far_future():
    assert parse_date("2030-01-01", reference=REF) is None


def test_build_record_requires_title_and_url():
    assert build_record(
        company="X", title=None, job_url="https://e.com/j/1",
        ats_provider="workday", scraping_method="direct_api",
    ) is None
    assert build_record(
        company="X", title="Data Engineer", job_url=None,
        ats_provider="workday", scraping_method="direct_api",
    ) is None
