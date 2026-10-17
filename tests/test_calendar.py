import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon.calendar_sync import event_body, push_confirmed, schedule_event
from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.schema import ActionItem, Extraction
from doraemon.web import create_app

SG = "Asia/Singapore"


def item(type="bill", title="SIT tuition fees", start=None, all_day=True, tz=SG, end=None, **extra):
    start = start or datetime(2026, 10, 27, tzinfo=ZoneInfo(tz))
    return ActionItem(message_id="m", type=type, title=title, start_at=start, end_at=end, all_day=all_day,
                      timezone=tz, evidence_snippet="Due date for fee payment is on 27 Oct 2026.",
                      confidence=0.9, **extra)


class FakeCalendar:
    def __init__(self):
        self.events, self.deleted, self.fail = {}, [], None

    def upsert(self, event_id, body):
        if self.fail:
            raise RuntimeError(self.fail)
        event_id = event_id or f"evt{len(self.events) + 1}"
        self.events[event_id] = body
        return event_id

    def delete(self, event_id):
        self.deleted.append(event_id)
        self.events.pop(event_id, None)


def test_bill_is_all_day_with_reminders_and_email_link():
    body = event_body(item(amount=Decimal("1200.00"), currency="SGD"), 7, "g123")
    assert body["start"] == {"date": "2026-10-27"} and body["end"] == {"date": "2026-10-28"}
    assert body["summary"] == "💸 SIT tuition fees"
    assert "SGD 1200.00" in body["description"]
    assert body["source"]["url"].endswith("#all/g123")
    assert {r["minutes"] for r in body["reminders"]["overrides"]} == {900, 3780}
    assert body["extendedProperties"]["private"]["doraemon_item_id"] == "7"


def test_flight_keeps_departure_timezone():
    narita = datetime(2026, 4, 24, 16, 50, tzinfo=ZoneInfo("Asia/Tokyo"))
    body = event_body(item("flight", "ZG053 Narita -> Changi", narita, all_day=False, tz="Asia/Tokyo",
                           departs_from="Narita International"), 1, "g")
    assert body["start"] == {"dateTime": "2026-04-24T16:50:00+09:00", "timeZone": "Asia/Tokyo"}
    assert body["end"]["dateTime"] == "2026-04-24T18:50:00+09:00"
    assert "Departs: Narita International" in body["description"]


def test_hotel_spans_check_in_to_check_out():
    check_in = datetime(2026, 4, 18, tzinfo=ZoneInfo(SG))
    body = event_body(item("hotel", "Hotel S-Plus Hiroshima", check_in, end=check_in + timedelta(days=2)), 1, "g")
    assert body["start"] == {"date": "2026-04-18"} and body["end"] == {"date": "2026-04-21"}


def test_item_without_date_is_refused():
    no_date = item().model_copy(update={"start_at": None})
    with pytest.raises(ValueError):
        event_body(no_date, 1, "g")


# --- through the web app ------------------------------------------------------

def email():
    return ParsedEmail(message_id="m", subject="Fee Statement", sender="SIT <fin@sit.edu.sg>",
                       sent_at=datetime.now(timezone.utc), body="")


@pytest.fixture
def app_with(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone=SG,
                            google_token=str(tmp_path / "token.json"),
                            google_credentials=str(tmp_path / "credentials.json"))
    db = Database(s.db_path)
    soon = datetime.now(ZoneInfo(SG)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=5)
    # appointments still go to Google Calendar; bills get Telegram reminders (tests/test_reminders.py)
    db.save_result("g1", "t1", email(), Extraction(message_id="m", items=[item("appointment", "Dentist", start=soon)]),
                   "extracted")

    def make(cal, schedule=None):
        return TestClient(create_app(s, calendar=cal, schedule=schedule), follow_redirects=True), db
    return make


def test_confirm_adds_event_and_undo_removes_it(app_with):
    cal = FakeCalendar()
    client, db = app_with(cal)
    msg = client.post("/items/1/confirm", headers={"x-requested-with": "fetch"}).json()["html"]
    assert "Added “Dentist” to your Doraemon calendar" in msg
    assert db.get("item", 1)["calendar_event_id"] == "evt1"
    assert "✓ In calendar" in client.get("/upcoming").text

    client.post("/undo/1")
    assert cal.deleted == ["evt1"]
    assert db.get("item", 1)["calendar_event_id"] is None
    assert db.get("item", 1)["status"] == "proposed"


def test_not_connected_still_confirms_and_explains(app_with):
    client, db = app_with(None)  # no token in tmp_path, so Calendar isn't connected
    msg = client.post("/items/1/confirm", headers={"x-requested-with": "fetch"}).json()["html"]
    assert "python -m doraemon.calendar_sync connect" in msg
    assert db.get("item", 1)["status"] == "confirmed"
    assert "Add to calendar" in client.get("/upcoming").text


def test_calendar_error_is_reported_and_retry_works(app_with):
    cal = FakeCalendar()
    cal.fail = "Google Calendar API has not been used in project 123"
    client, db = app_with(cal)
    msg = client.post("/items/1/confirm", headers={"x-requested-with": "fetch"}).json()["html"]
    assert "the Google Calendar API is turned off" in msg
    assert db.get("item", 1)["calendar_event_id"] is None
    cal.fail = None
    client.post("/items/1/sync")
    assert db.get("item", 1)["calendar_event_id"] == "evt1"


def test_push_adds_confirmed_items_missing_from_calendar(tmp_path):
    db = Database(tmp_path / "d.db")
    db.save_result("g1", "t1", email(), Extraction(message_id="m", items=[item("appointment", "Dentist"), item()]),
                   "extracted")
    db.update("item", 1, "confirm", status="confirmed")
    db.update("item", 2, "confirm", status="confirmed")
    cal = FakeCalendar()
    assert push_confirmed(db, cal) == (1, 0)  # the bill has Telegram reminders instead
    assert [b["summary"] for b in cal.events.values()] == ["📌 Dentist"]
    assert push_confirmed(db, cal) == (0, 0)  # already there: nothing added twice


# --- your week, read from all your Google calendars ------------------------------

def test_google_events_become_week_entries():
    sg = ZoneInfo(SG)
    trip = schedule_event({"summary": "Bali", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-12"},
                           "htmlLink": "https://www.google.com/calendar/event?eid=x"}, "Personal", "#9fe1e7", sg)
    assert (trip["first"], trip["last"], trip["start"]) == ("2026-10-09", "2026-10-11", "")  # end date is exclusive
    call = schedule_event({"summary": "Standup", "start": {"dateTime": "2026-10-06T01:00:00Z"},
                           "end": {"dateTime": "2026-10-06T01:30:00Z"}}, "Work", "red", sg)
    assert (call["first"], call["start"], call["end"]) == ("2026-10-06", "09:00", "09:30")  # in your timezone
    assert call["color"] == "#9AA0A6"  # anything but a hex colour is replaced
    declined = {"summary": "x", "start": {"date": "2026-10-06"}, "attendees": [{"self": True, "responseStatus": "declined"}]}
    assert schedule_event(declined, "Work", "#000000", sg) is None
    assert schedule_event({"status": "cancelled", "start": {"date": "2026-10-06"}}, "Work", "#000000", sg) is None
    sneaky = schedule_event({"summary": "x", "start": {"date": "2026-10-06"}, "htmlLink": "javascript:alert(1)"}, "W", "", sg)
    assert sneaky["link"] == ""


class FakeSchedule:
    def __init__(self, events):
        self.events_list, self.asked = events, []

    def events(self, first, last):
        self.asked.append((first, last))
        return [dict(e) for e in self.events_list]


def test_dorae2_shows_your_week(app_with):
    today = datetime.now(ZoneInfo(SG)).date()
    schedule = FakeSchedule([
        {"title": "<b>Dentist</b>", "first": today.isoformat(), "last": today.isoformat(), "start": "14:30",
         "end": "15:00", "calendar": "Personal", "color": "#7986CB", "location": "", "link": "https://calendar.google.com/x"},
    ])
    client, _ = app_with(None, schedule)
    client.headers["x-requested-with"] = "fetch"
    html = client.post("/chat/calendar/ask", data={"q": "can you show me my schedule for this week"}).json()["html"]
    monday = today - timedelta(days=today.weekday())
    assert schedule.asked == [(monday, monday + timedelta(days=6))]
    assert "week-card" in html and html.count('class="day-head"') == 7
    assert "&lt;b&gt;Dentist&lt;/b&gt;" in html and "2:30pm" in html and 'href="https://calendar.google.com/x"' in html
    assert 'class="day today' in html
    client.post("/chat/calendar/ask", data={"q": "what about next week?"})
    assert schedule.asked[-1][0] == monday + timedelta(days=7)


def test_week_includes_email_items_not_in_google_yet(app_with):
    schedule = FakeSchedule([])
    client, db = app_with(None, schedule)
    client.headers["x-requested-with"] = "fetch"
    due = datetime.fromisoformat(db.get("item", 1)["start_at"]).astimezone(ZoneInfo(SG)).date()  # in 5 days
    today = datetime.now(ZoneInfo(SG)).date()
    sunday = today + timedelta(days=6 - today.weekday())
    q = "show my schedule this week" if due <= sunday else "my schedule next week"
    html = client.post("/chat/calendar/ask", data={"q": q}).json()["html"]
    assert "Dentist" in html and "ev-waiting" in html and "still needs your OK" in html


def test_week_asks_to_connect_when_reading_isnt_approved(app_with):
    client, _ = app_with(None)  # no token, so reading calendars isn't approved
    client.headers["x-requested-with"] = "fetch"
    html = client.post("/chat/calendar/ask", data={"q": "show my schedule"}).json()["html"]
    assert "python -m doraemon.calendar_sync connect" in html


# --- reading your calendars quickly ---------------------------------------------------------

class FakeGoogle:
    """calendarList / events / batch, counting round trips to Google."""

    def __init__(self, calendars=3):
        self.trips, self.cals = 0, [{"id": f"c{i}", "summary": f"Cal {i}", "selected": True} for i in range(calendars)]

    def calendarList(self):
        return self

    def list(self, pageToken=None, **query):
        return _Call(self, {"items": self.cals} if "calendarId" not in query else
                     {"items": [{"id": f"e-{query['calendarId']}", "summary": f"Event in {query['calendarId']}",
                                 "start": {"dateTime": "2026-10-20T09:00:00+08:00"},
                                 "end": {"dateTime": "2026-10-20T10:00:00+08:00"}}]})

    def events(self):
        return self

    def new_batch_http_request(self):
        return _Batch(self)


class _Call:
    def __init__(self, google, answer):
        self.google, self.answer = google, answer

    def execute(self):
        self.google.trips += 1
        return self.answer


class _Batch:
    def __init__(self, google):
        self.google, self.calls = google, []

    def add(self, call, callback):
        self.calls.append((call, callback))

    def execute(self):
        self.google.trips += 1  # one round trip for the whole batch
        for n, (call, callback) in enumerate(self.calls):
            callback(str(n), call.answer, None)


def schedule(google):
    import threading
    from doraemon.calendar_sync import GoogleSchedule
    s = GoogleSchedule.__new__(GoogleSchedule)
    s.service, s.zone, s.lock, s.listed, s.read = google, ZoneInfo("Asia/Singapore"), threading.Lock(), None, {}
    return s


def test_all_calendars_are_read_in_one_batch_and_remembered(monkeypatch):
    from doraemon import calendar_sync
    google = FakeGoogle(calendars=7)
    s = schedule(google)
    week = (date(2026, 10, 19), date(2026, 10, 25))
    events = s.events(*week)
    assert len(events) == 7 and google.trips == 2  # the calendar list, then one batch for all 7
    assert {e["id"] for e in events} == {f"e-c{i}" for i in range(7)}
    events.append({"title": "mine"})  # the caller's own list
    assert len(s.events(*week)) == 7 and google.trips == 2  # remembered: no trip at all
    s.events(date(2026, 10, 26), date(2026, 11, 1))
    assert google.trips == 3  # another week: one batch (the calendar list is remembered too)
    later = datetime.now(timezone.utc) + calendar_sync.EVENTS_FOR
    monkeypatch.setattr(calendar_sync, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: later)}))
    s.events(*week)
    assert google.trips == 4  # a few minutes on, it reads again


def test_an_event_confirmed_since_the_calendars_were_read_still_shows(tmp_path, settings):
    from doraemon.agents import Brain
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone="Asia/Singapore")
    db = Database(s.db_path)
    day = datetime.now(ZoneInfo("Asia/Singapore")).replace(hour=19, minute=0, second=0, microsecond=0) + timedelta(days=1)
    for gmail_id, title, event_id in (("g1", "Dentist", "ev-dentist"), ("g2", "Date night", "ev-new")):
        item = ActionItem(message_id="m", type="appointment", title=title, start_at=day, end_at=None, all_day=False,
                          timezone="Asia/Singapore", evidence_snippet="", confidence=0.9)
        email = ParsedEmail(message_id="m", subject="s", sender="a@b.c", sent_at=day, body="")
        db.save_result(gmail_id, "t", email, Extraction(message_id="m", items=[item]), "extracted")
        item_id = db.conn.execute("SELECT MAX(id) FROM action_items").fetchone()[0]
        db.update("item", item_id, "confirm", status="confirmed")
        db.set_calendar_event(item_id, event_id)

    class ReadEarlier:  # Google's answer from before "Date night" was added
        def events(self, first, last):
            return [{"title": "📌 Dentist", "first": day.date().isoformat(), "last": day.date().isoformat(),
                     "start": "19:00", "end": "20:00", "calendar": "Doraemon", "color": "#000000", "location": "",
                     "link": "", "id": "ev-dentist"}]
    brain = Brain(db, s, None, schedule=lambda: ReadEarlier())
    events, _ = brain.calendar_events(day.date(), day.date())
    assert sorted(e["title"] for e in events) == ["Date night", "📌 Dentist"]  # each once
