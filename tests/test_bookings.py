"""Screenshots of bookings for Dorae-2: a hotel, train tickets, read by the model and drafted for your calendar."""
import dataclasses
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon.extract import IMAGE_PROMPT, extract_image
from doraemon.jobs import Runtime
from doraemon.schema import ActionItem
from doraemon.web import create_app

SG = ZoneInfo("Asia/Singapore")
NEXT_YEAR = datetime.now(SG).year + 1
STAY = f"{datetime(NEXT_YEAR, 11, 5):%a} 5 – {datetime(NEXT_YEAR, 11, 7):%a} 7 Nov {NEXT_YEAR} (3 days)"


def raw(type="hotel", title="Dali Cang'er Hotel", when=f"5 Nov {NEXT_YEAR}", end=f"7 Nov {NEXT_YEAR}", **extra):
    return {"type": type, "title": title, "departs_from": None, "when": when, "end": end, "timezone": None,
            "location": "Dali", "booking_ref": "12345678", "amount": 210.0, "currency": "CNY",
            "evidence": "Check-in 5 Nov · Check-out 7 Nov", "confidence": 0.9, **extra}


class FakeVision:
    """The model that can see: bookings for the screenshot prompt, `food` (or "neither") for the food one."""
    name = "fake"

    def __init__(self, items=None, food="neither"):
        self.items = [raw()] if items is None else items
        self.food, self.calls = food, []

    def complete_json(self, system, user, schema, images=None):
        self.calls.append((system, images))
        if system == IMAGE_PROMPT:
            return json.dumps({"items": self.items, "transactions": []})
        return json.dumps({"kind": self.food, "product": "", "basis": "serving", "serving_size": "", "pack_size": "",
                           "kcal": 0, "kj": 0, "protein": 0, "carbs": 0, "fat": 0, "sugar": 0, "foods": []})


class FakeCalendar:
    def __init__(self):
        self.bodies = []

    def upsert(self, event_id, body):
        self.bodies.append(body)
        return event_id or "ev1"

    def delete(self, event_id):
        pass


@pytest.fixture
def rt(tmp_path, settings):
    rt = Runtime(dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone="Asia/Singapore",
                                     poll_minutes=0, google_token=str(tmp_path / "t.json"),
                                     google_credentials=str(tmp_path / "c.json")), calendar=FakeCalendar())
    rt.brain.food_backend = FakeVision()
    return rt


def test_a_hotel_screenshot_reads_as_a_stay_and_a_train_in_its_stations_time(settings):
    vision = FakeVision([raw(), raw("other_travel", "Train D8761 Kunming → Dali", f"5 Nov {NEXT_YEAR} 08:15",
                                    f"5 Nov {NEXT_YEAR} 10:20", departs_from="Kunming", timezone="Asia/Shanghai")])
    found = extract_image(b"png", "my dali trip", vision, settings, datetime.now(SG))
    hotel, train = found.items
    assert hotel.type == "hotel" and hotel.all_day and (hotel.start_at.day, hotel.end_at.day) == (5, 7)
    assert train.timezone == "Asia/Shanghai" and train.start_at.strftime("%H:%M") == "08:15" and not train.all_day
    assert vision.calls[0][1] == [b"png"]  # the model saw the image


def test_no_booking_in_the_screenshot_is_said(rt):
    rt.brain.food_backend = FakeVision([])
    card = rt.brain.booking_reply(b"png")
    assert card["kind"] == "text" and card["text"].startswith("I couldn't find a booking with a date")


def test_a_screenshot_drafts_the_booking_for_confirm(rt):
    card = rt.brain.booking_reply(b"png")
    assert card["kind"] == "items"
    assert card["text"] == (f"From your screenshot:\n• Dali Cang'er Hotel: {STAY}\n"
                            "Check the dates, then press Confirm to add them to your calendar.")
    row = rt.db.get("item", card["payload"]["ids"][0])
    assert row["status"] == "proposed" and row["type"] == "hotel" and row["booking_ref"] == "12345678"


def test_a_booking_you_already_have_is_brought_up_to_date(rt):
    """The hotel you typed as one day ("as travel to"), already in Google Calendar: the screenshot fixes its dates."""
    first = datetime(NEXT_YEAR, 11, 5, tzinfo=SG)
    item_id = rt.db.add_item("chat:abc", ActionItem(
        message_id="chat", type="appointment", title="Dali Cang'er Hotel as travel to", start_at=first, end_at=None,
        all_day=True, timezone="Asia/Singapore", evidence_snippet="q", confidence=1.0))
    rt.db.update("item", item_id, "confirm", status="confirmed")
    rt.db.set_calendar_event(item_id, "ev9")
    card = rt.brain.booking_reply(b"png")
    assert card["payload"]["ids"] == [item_id]  # no second copy
    assert card["text"] == (f"From your screenshot:\n• Dali Cang'er Hotel: {STAY} (you had it already; I updated it to "
                            "match, in Google Calendar too)")
    row = rt.db.get("item", item_id)
    assert row["title"] == "Dali Cang'er Hotel" and row["end_at"].startswith(f"{NEXT_YEAR}-11-07")
    assert row["type"] == "hotel" and row["booking_ref"] == "12345678"
    assert rt.get_calendar().bodies[-1]["end"] == {"date": f"{NEXT_YEAR}-11-08"}
    assert "Confirm" not in card["text"]  # nothing waiting


def test_dorae2s_chat_takes_a_screenshot(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), google_token=str(tmp_path / "t.json"),
                            google_credentials=str(tmp_path / "c.json"))
    client = TestClient(create_app(s), follow_redirects=True)
    client.app.state.runtime.brain.food_backend = FakeVision()
    assert 'id="ask-photo"' in client.get("/chat/calendar").text
    html = client.post("/chat/calendar/photo", files={"photo": ("s.png", b"png", "image/png")}, data={"q": ""},
                       headers={"x-requested-with": "fetch"}).json()["html"]
    assert "From your screenshot" in html and "Dali Cang&#39;er Hotel" in html and "/items/1/confirm" in html
    assert client.post("/chat/money/photo", files={"photo": ("s.png", b"png", "image/png")}).status_code == 404


def test_a_booking_screenshot_on_telegram_gets_add_buttons(rt):
    rt.brain.food_backend = vision = FakeVision([raw(), raw("other_travel", "Train Kunming → Dali",
                                                            f"5 Nov {NEXT_YEAR} 08:15", None, timezone="Asia/Shanghai")])
    text, buttons = rt.phone.photo(b"png", "hotel and train")  # the caption says so: no food reading first
    assert [c[0] for c in vision.calls] == [IMAGE_PROMPT]
    assert text.startswith("📷 From your screenshot:") and "press Add to put them in your calendar" in text
    assert buttons[0][0][0] == "✅ Add Dali Cang'er Hotel" and buttons[2][0][0] == "✅ Add all"
    said = rt.button(buttons[2][0][1])
    assert said.count("Added") == 2 and len(rt.get_calendar().bodies) == 2
    assert "From a screenshot on Telegram" in rt.db.conn.execute(
        "SELECT text FROM chat_messages WHERE agent = 'calendar' ORDER BY id LIMIT 1").fetchone()[0]


def test_a_photo_without_a_caption_thats_not_food_is_read_as_a_booking(rt):
    text, buttons = rt.phone.photo(b"png")
    assert len(rt.brain.food_backend.calls) == 2 and text.startswith("📷 From your screenshot:") and buttons
