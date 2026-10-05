from datetime import datetime, timezone

from doraemon.dates import resolve

TZ = "America/New_York"
SENT = datetime(2026, 10, 4, 9, 15, tzinfo=timezone.utc)  # a Sunday


def test_full_date_with_time():
    dt, has_time = resolve("Friday, October 16, 2026 at 3:30 PM", SENT, TZ)
    assert has_time
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 10, 16, 15, 30)
    assert str(dt.tzinfo) == TZ


def test_date_only_is_all_day():
    dt, has_time = resolve("Oct 16, 2026", SENT, TZ)
    assert not has_time
    assert (dt.month, dt.day, dt.hour) == (10, 16, 0)


def test_relative_dates_use_email_sent_date():
    dt, _ = resolve("in 3 days", SENT, TZ)
    assert dt.date().isoformat() == "2026-10-07"


def test_yearless_past_date_is_not_pushed_to_next_year():
    dt, _ = resolve("Oct 1", SENT, TZ)
    assert dt.date().isoformat() == "2026-10-01"


def test_numeric_dates_follow_date_order():
    assert resolve("02/10/26 16:23", SENT, TZ)[0].date().isoformat() == "2026-10-02"
    assert resolve("02/10/26 16:23", SENT, TZ, date_order="MDY")[0].date().isoformat() == "2026-02-10"


def test_iso_dates_ignore_date_order():
    dt, has_time = resolve("2026-10-02T22:12GMT+08:00", SENT, "Asia/Singapore")
    assert (dt.month, dt.day, dt.hour, has_time) == (10, 2, 22, True)
    dt, has_time = resolve("2026-10-02", SENT, TZ)
    assert (dt.month, dt.day, has_time) == (10, 2, False)


def test_year_first_slash_dates():
    dt, has_time = resolve("2026/04/10 (Fri) 00:30", SENT, TZ)
    assert (dt.month, dt.day, dt.hour, dt.minute, has_time) == (4, 10, 0, 30, True)
    dt, has_time = resolve("2026/04/24", SENT, TZ)
    assert (dt.month, dt.day, has_time) == (4, 24, False)


def test_garbage_and_empty():
    assert resolve("whenever you like", SENT, TZ) == (None, False)
    assert resolve(None, SENT, TZ) == (None, False)
