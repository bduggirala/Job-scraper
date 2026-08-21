from datetime import datetime, timedelta, timezone

from filters import DATE_UNAVAILABLE, OUTSIDE_WINDOW, WITHIN_WINDOW, classify_date

REF = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


def test_status_labels_are_window_agnostic():
    assert WITHIN_WINDOW == "within_window"
    assert OUTSIDE_WINDOW == "older_than_window"
    assert DATE_UNAVAILABLE == "date_unavailable"


def test_classify_date_respects_hours_old():
    record = {"date_posted": (REF - timedelta(hours=100)).isoformat()}
    assert classify_date(record, hours_old=72, now=REF) == OUTSIDE_WINDOW
    assert classify_date(record, hours_old=168, now=REF) == WITHIN_WINDOW


def test_classify_date_without_any_date():
    assert classify_date({}, hours_old=168, now=REF) == DATE_UNAVAILABLE
