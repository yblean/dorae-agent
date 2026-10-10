"""Dorae-2's morning briefing: what goes in it, and when it's posted."""
import dataclasses
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon.agents import Brain
from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.schema import ActionItem, Extraction
from doraemon.web import create_app

TZ = "Asia/Singapore"


class FakeSchedule:
    def __init__(self, events=None, error=None):
        self.list, self.error = events or [], error

    def events(self, first, last):
        if self.error:
            raise RuntimeError(self.error)
        return [e for e in self.list if first.isoformat() <= e["first"] and e["last"] <= last.isoformat()]


def gcal(title, day, start="", end="", calendar="Personal"):
    return {"title": title, "first": day.isoformat(), "last": day.isoformat(), "start": start, "end": end,
            "calendar": calendar, "color": "#039BE5", "location": "", "link": "https://calendar.google.com/x"}


@pytest.fixture
def setup(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone=TZ, home_currency="SGD",
                            briefing_time="08:00", google_token=str(tmp_path / "t.json"),
                            google_credentials=str(tmp_path / "c.json"))
    db = Database(s.db_path)
    now = datetime.now(ZoneInfo(TZ))
    noon = lambda days: now.replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=days)

    def add(gmail_id, type, title, days, all_day=False, amount=None, status="proposed"):
        item = ActionItem(message_id="m", type=type, title=title, start_at=noon(days), end_at=None, all_day=all_day,
                          timezone=TZ, amount=Decimal(amount) if amount else None, currency="SGD" if amount else None,
                          evidence_snippet="", confidence=0.9)
        email = ParsedEmail(message_id="m", subject="s", sender="Shop <a@shop.sg>", sent_at=now, body="")
        db.save_result(gmail_id, "t", email, Extraction(message_id="m", items=[item]), "extracted")
        item_id = db.conn.execute("SELECT MAX(id) FROM action_items").fetchone()[0]
        if status != "proposed":
            db.update("item", item_id, "confirm", status=status)
        return item_id
    return s, db, add, now


def test_briefing_covers_today_tomorrow_bills_trips_and_what_needs_ok(setup):
    s, db, add, now = setup
    add("g1", "appointment", "Dentist", 0, status="confirmed")
    add("g2", "bill", "SP electricity", 3, all_day=True, amount="80.50")
    add("g3", "bill", "Phone bill", 5, all_day=True, amount="40", status="confirmed")
    add("g4", "flight", "Flight to Tokyo", 6)
    add("g5", "deadline", "Module registration", 1)
    schedule = FakeSchedule([gcal("Gym", now.date(), "07:30", "08:30")])
    brain = Brain(db, s, schedule=lambda: schedule)
    [brief, waiting] = brain.briefing()
    text = brief["text"]
    assert f"Here's {now:%A} {now.day} {now:%B}." in text
    assert "2 things today, starting with Gym at 7:30am." in text  # Google event + Dentist (from email)
    assert "1 tomorrow." in text
    assert "2 bills due in the next 7 days (SGD 120.50)." in text
    assert "Trip coming up: Flight to Tokyo" in text
    assert "3 things from your email need your OK." in text  # 3 proposed: electricity, flight, registration
    today, tomorrow = brief["payload"]["days"]
    assert [e["title"] for e in today["events"]] == ["Gym", "Dentist"]  # by time: 7:30, then 12:00
    assert [e["title"] for e in tomorrow["events"]] == ["Module registration"]
    assert len(brief["payload"]["bills"]) == 2 and len(brief["payload"]["trips"]) == 1
    assert waiting["kind"] == "items" and len(waiting["payload"]["ids"]) == 3


def test_quiet_day_and_calendar_trouble(setup):
    s, db, add, now = setup
    brain = Brain(db, s, schedule=lambda: FakeSchedule(error="quota exceeded"))
    [brief] = brain.briefing()  # nothing needs your OK: no second message
    assert "Nothing on today. 🌤️" in brief["text"] and "couldn't read your Google Calendar: quota exceeded" in brief["text"]


def test_briefing_works_without_google_access(setup):
    s, db, add, now = setup
    add("g1", "appointment", "Dentist", 0, status="confirmed")
    [brief] = Brain(db, s, schedule=lambda: None).briefing()
    assert "1 thing today" in brief["text"] and "Google" not in brief["text"]


def test_telegram_reminders_going_out_today_are_listed(setup):
    s, db, add, now = setup
    item_id = add("g1", "bill", "SP electricity", 2, all_day=True, status="confirmed")
    later = now + timedelta(minutes=30)
    if later.date() != now.date():
        pytest.skip("too close to midnight for a reminder later today")
    db.set_reminders(item_id, [{"at": later, "label": "soon"}, {"at": now + timedelta(days=1), "label": "1 day"}])
    [brief] = Brain(db, s).briefing()
    assert "1 Telegram reminder going out today." in brief["text"]
    assert brief["payload"]["reminders"] == [{"time": later.strftime("%H:%M"), "title": "SP electricity"}]


def test_posted_at_its_time_once_a_day(setup):
    s, db, add, now = setup
    brain = Brain(db, s)
    today = now.date()
    assert not brain.morning_tick(datetime.combine(today, time(7, 59), ZoneInfo(TZ)))
    assert db.last_message("calendar") is None
    assert brain.morning_tick(datetime.combine(today, time(8, 0), ZoneInfo(TZ)))
    assert not brain.morning_tick(datetime.combine(today, time(8, 1), ZoneInfo(TZ)))
    assert [r["kind"] for r in db.messages("calendar")] == ["briefing"]


def test_opening_the_chat_before_the_briefing_waits_for_it(setup):
    s, db, add, now = setup
    db.add_message("calendar", "agent", "yesterday's news")
    late = Brain(db, dataclasses.replace(s, briefing_time="23:59:59"))
    assert not late.ensure_overview("calendar")  # the chat has messages: wait for the briefing
    db.clear_messages("calendar")
    assert late.ensure_overview("calendar")  # an empty chat (first use, New chat) gets one straight away


def test_briefing_off_posts_when_you_open_the_chat(setup):
    s, db, add, now = setup
    db.add_message("calendar", "agent", "yesterday's news")
    off = Brain(db, dataclasses.replace(s, briefing_time="off"))
    assert not off.morning_tick() and off.ensure_overview("calendar")


def test_briefing_card_and_routine_in_the_web_app(setup):
    s, db, add, now = setup
    add("g1", "appointment", "Dentist <b>", 0, status="confirmed")
    add("g2", "bill", "SP electricity", 3, all_day=True, amount="80.50")
    client = TestClient(create_app(s, schedule=FakeSchedule([gcal("Gym", now.date(), "07:30", "08:30")])))
    page = client.get("/chat/calendar").text
    assert 'class="card briefing"' in page and "Bills due this week" in page
    assert "7:30am–8:30am" in page and "Dentist &lt;b&gt;" in page
    assert "Every day, 8am" in page
    off = TestClient(create_app(dataclasses.replace(s, briefing_time="off")))
    assert "Every day · turned off" in off.get("/chat/calendar").text
