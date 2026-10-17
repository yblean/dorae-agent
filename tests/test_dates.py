from datetime import datetime, timezone

import pytest

from doraemon.dates import resolve, resolve_spoken

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


@pytest.mark.parametrize("text, expected, has_time", [
    ("2026-11-05 at 06:30", (11, 5, 6, 30), True),  # China Eastern tickets: read as 11 May before
    ("2026-11-14 at 00:55", (11, 14, 0, 55), True),
    ("2026-11-05 06:30", (11, 5, 6, 30), True),
    ("2026-11-05 at 6:30pm", (11, 5, 18, 30), True),
    ("2026-11-05 (Thu) 7pm", (11, 5, 19, 0), True),
    ("2026-11-05 (Thu)", (11, 5, 0, 0), False),
])
def test_year_first_dates_with_words_never_follow_date_order(text, expected, has_time):
    dt, timed = resolve(text, SENT, TZ, date_order="DMY")
    assert ((dt.month, dt.day, dt.hour, dt.minute), timed) == (expected, has_time)


def test_garbage_and_empty():
    assert resolve("whenever you like", SENT, TZ) == (None, False)
    assert resolve(None, SENT, TZ) == (None, False)


# --- dates typed in chat ------------------------------------------------------------------

@pytest.mark.parametrize("text, expected, has_time", [
    ("tmr", "Sun 11 Oct 00:00", False),
    ("tmr 5pm", "Sun 11 Oct 17:00", True),
    ("in 2 hours", "Sat 10 Oct 17:00", True),
    ("next monday at 9am", "Mon 12 Oct 09:00", True),  # dateparser alone reads this as last Friday
    ("8pm", "Sat 10 Oct 20:00", True),                  # a bare time later today stays today...
    ("2pm", "Sun 11 Oct 14:00", True),                  # ...and one that has passed is tomorrow
    ("by friday", "Fri 16 Oct 00:00", False),
    ("on 1/11 9am", "Sun 01 Nov 09:00", True),
    ("sometime soon", None, False),
])
def test_chat_dates(text, expected, has_time):
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 10, 15, 0, tzinfo=ZoneInfo("Asia/Singapore"))
    dt, timed = resolve_spoken(text, now, "Asia/Singapore")
    assert (dt.strftime("%a %d %b %H:%M") if dt else None, timed) == (expected, has_time)
