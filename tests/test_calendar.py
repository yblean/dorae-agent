import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon.calendar_sync import event_body, push_confirmed
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
    db.save_result("g1", "t1", email(), Extraction(message_id="m", items=[item(start=soon)]), "extracted")

    def make(cal):
        return TestClient(create_app(s, calendar=cal), follow_redirects=True), db
    return make


def test_confirm_adds_event_and_undo_removes_it(app_with):
    cal = FakeCalendar()
    client, db = app_with(cal)
    msg = client.post("/items/1/confirm", headers={"x-requested-with": "fetch"}).json()["html"]
    assert "Added “SIT tuition fees” to your Doraemon calendar" in msg
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
    db.save_result("g1", "t1", email(), Extraction(message_id="m", items=[item()]), "extracted")
    db.update("item", 1, "confirm", status="confirmed")
    cal = FakeCalendar()
    assert push_confirmed(db, cal) == (1, 0)
    assert push_confirmed(db, cal) == (0, 0)  # already there: nothing added twice
