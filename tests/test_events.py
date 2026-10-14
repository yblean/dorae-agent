"""Calendar events asked for in your own words, read by fixed rules (doraemon.events)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from doraemon.db import Database, item_model
from doraemon.events import EventAsk, draft_event, split_event

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=SG)  # a Wednesday


@pytest.mark.parametrize("text, expected", [
    ("add date night on 26/10 19:00", EventAsk("Date night", "on 26/10 19:00")),
    ("add date night on 26/10 7-9pm", EventAsk("Date night", "on 26/10 7pm", "9pm")),
    ("add dinner with Sam tmr 7-9pm to my calendar", EventAsk("Dinner with Sam", "tomorrow 7pm", "9pm")),
    ("schedule dentist next monday at 3pm", EventAsk("Dentist", "next monday at 3pm")),
    ("put gym on my calendar tomorrow 7am", EventAsk("Gym", "tomorrow 7am")),
    ("add an event: team lunch friday 12:30 to 2pm", EventAsk("Team lunch", "friday 12:30", "2pm")),
    ("add an appointment for physio wed 4:30pm", EventAsk("Physio", "wed 4:30pm")),
    ("add meeting with prof thu 2pm", EventAsk("Meeting with prof", "thu 2pm")),
    ("add flight to tokyo on 20/11 11-1pm", EventAsk("Flight to tokyo", "on 20/11 11am", "1pm")),
    ("add party from 10pm until 2am saturday", EventAsk("Party", "10pm saturday", "2am")),
    ("add trip to KL 20/11 to 23/11", EventAsk("Trip to KL", "20/11", "23/11")),
    ("event: haircut", EventAsk("Haircut", "")),  # says it's an event, so the bot asks when
    ("make dinner tomorrow", None),
    ("add 300 to my dining budget", None),
    ("set my dining budget to 300", None),
    ("add a reminder to pay rent tmr", None),  # a reminder, not an event
    ("how much did I spend this week", None),
])
def test_event_requests_split_into_title_when_and_end(text, expected):
    assert split_event(text) == expected


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "d.db")


def drafted(db, *args, **kw):
    result, item_id = draft_event(db, "Asia/Singapore", "DMY", *args, "q", NOW, **kw)
    return result, item_model(db.get("item", item_id)) if item_id else None


def test_a_time_range_sets_the_end(db):
    _, item = drafted(db, "Date night", "on 26/10 7pm", end="9pm")
    assert item.start_at.astimezone(SG).strftime("%d %H:%M") == "26 19:00"
    assert item.end_at.astimezone(SG).strftime("%d %H:%M") == "26 21:00" and not item.all_day
    assert item.type == "appointment"


def test_an_end_after_midnight_is_the_next_day(db):
    _, item = drafted(db, "Party", "saturday 10pm", end="2am")
    assert item.end_at.astimezone(SG).strftime("%a %H:%M") == "Sun 02:00"


def test_days_make_an_all_day_event_through_the_last_one(db):
    _, item = drafted(db, "Trip to KL", "20/11", end="23/11")
    assert item.all_day and item.end_at.astimezone(SG).strftime("%d %b") == "23 Nov"


def test_without_a_time_its_all_day_and_past_dates_are_refused(db):
    _, item = drafted(db, "Sam bday", "26/10")
    assert item.all_day and item.end_at is None
    result, item = drafted(db, "Dentist", "13/10 3pm")
    assert item is None and "which has passed" in result["error"]
    result, _ = drafted(db, "Haircut", "")
    assert result["error"] == "When is “Haircut”? For example: tomorrow 3pm, or 26/10 7-9pm."


def test_asking_twice_finds_the_one_already_there(db):
    _, first = drafted(db, "Date night", "26/10 7pm")
    result, again = drafted(db, "date night", "26/10 7pm")
    assert result["already_there"] and db.conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 1
