"""Holding back suggestions from newsletters, mass mail and senders you always dismiss."""
import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon import quiet, senders
from doraemon.email_parse import ParsedEmail, parse_eml
from doraemon.jobs import Runtime
from doraemon.schema import ActionItem, Extraction
from doraemon.tidy import tidy
from doraemon.web import create_app

SG = ZoneInfo("Asia/Singapore")
NLB = "GoLibrary by NLB <edm@experience.nlb.gov.sg>"


class FakeBot:
    chat_id = "42"

    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))


@pytest.fixture
def settings(tmp_path, settings):
    return dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone="Asia/Singapore", home_currency="SGD",
                               poll_minutes=0, google_token=str(tmp_path / "t.json"),
                               google_credentials=str(tmp_path / "c.json"), user_emails=("me@example.com",))


def item(title, kind="appointment", days=5):
    start = datetime.now(SG).replace(hour=14, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return ActionItem(message_id="m", type=kind, title=title, start_at=start, end_at=None, all_day=False,
                      timezone="Asia/Singapore", evidence_snippet="Join us!", confidence=0.95)


def arrive(db, gmail_id, items, sender=NLB, bulk=False):
    """An email with these items, saved and held back as ingest does. Returns (how many held, the email)."""
    email = ParsedEmail(message_id="m", subject="s", sender=sender, sent_at=datetime.now(SG), body="", bulk=bulk)
    db.save_result(gmail_id, "t", email, Extraction(message_id="m", items=items), "extracted")
    return quiet.hold(db, gmail_id, email, ("me@example.com",)), email


def statuses(db, gmail_id):
    return [r["status"] for r in db.conn.execute("SELECT status FROM action_items WHERE gmail_id = ? ORDER BY id",
                                                 (gmail_id,))]


# --- reading mass mail -----------------------------------------------------------------------------

@pytest.mark.parametrize("headers, bulk", [
    ("List-Unsubscribe: <mailto:unsub@news.example>\n", True),
    ("Precedence: bulk\n", True),
    ("", False),
])
def test_mass_mail_is_noticed_from_its_headers(headers, bulk):
    raw = (f"From: a@b.example\nTo: me@example.com\nSubject: hi\n{headers}Content-Type: text/plain\n\nhello").encode()
    assert parse_eml(raw).bulk is bulk


# --- what's held back ------------------------------------------------------------------------------

def test_a_newsletter_full_of_events_is_held_but_its_flight_is_not(settings):
    db = Runtime(settings).db
    held, _ = arrive(db, "news", [item("Read Together"), item("Darwin's Finches"), item("Love Languages"),
                                  item("SQ 123 to Tokyo", kind="flight")])
    assert held == 3 and statuses(db, "news") == ["held", "held", "held", "proposed"]
    (first, *_) = Runtime(settings).brain.held()
    assert quiet.reason(db, first) == "a newsletter or schedule, with 4 things in one email"


def test_mass_mail_is_held_except_bills_and_deliveries(settings):
    db = Runtime(settings).db
    assert arrive(db, "a", [item("Enroll by Sept 17th", kind="deadline")], sender="Harvard <h@mail.example>",
                  bulk=True)[0] == 1
    assert arrive(db, "b", [item("Grab PayLater bill", kind="bill")], sender="Grab <no-reply@grab.example>",
                  bulk=True)[0] == 0
    assert arrive(db, "c", [item("Parcel arriving", kind="delivery")], sender="Shopee <s@shopee.example>",
                  bulk=True)[0] == 0
    assert quiet.reason(db, db.conn.execute("SELECT id FROM action_items WHERE gmail_id = 'a'").fetchone()[0]) \
        == "mass mail (it has an unsubscribe link)"


def test_a_sender_you_always_dismiss_is_held_until_you_confirm_one(settings):
    db = Runtime(settings).db
    sender = "CrowdTask <alerts@crowd.example>"
    for n in range(3):
        arrive(db, f"d{n}", [item(f"Survey {n}", kind="deadline")], sender=sender)
        db.update("item", db.conn.execute("SELECT MAX(id) FROM action_items").fetchone()[0], "dismiss",
                  status="dismissed")
    assert arrive(db, "d3", [item("Survey 3", kind="deadline")], sender=sender)[0] == 1
    held = db.conn.execute("SELECT id FROM action_items WHERE gmail_id = 'd3'").fetchone()[0]
    assert quiet.reason(db, held) == "you've dismissed all 3 things from CrowdTask"
    db.update("item", held, "confirm", status="confirmed")  # you wanted one after all: they're shown again
    assert arrive(db, "d4", [item("Survey 4", kind="deadline")], sender=sender)[0] == 0


def test_your_own_forwards_and_people_you_confirmed_are_never_held(settings):
    db = Runtime(settings).db
    three = [item("A"), item("B"), item("C")]
    assert arrive(db, "fwd", three, sender="Me <me@example.com>", bulk=True)[0] == 0
    arrive(db, "trip", [item("Hotel", kind="hotel")], sender="Trip <t@trip.example>")
    db.update("item", db.conn.execute("SELECT id FROM action_items WHERE gmail_id = 'trip'").fetchone()[0], "confirm",
              status="confirmed")
    assert arrive(db, "trip2", [item("Tour"), item("Dinner"), item("Spa")], sender="Trip <t@trip.example>",
                  bulk=True)[0] == 0


def test_reading_an_email_again_replaces_its_held_items(settings):
    db = Runtime(settings).db
    arrive(db, "news", [item("A"), item("B"), item("C")])
    arrive(db, "news", [item("A"), item("B"), item("C")])
    assert statuses(db, "news") == ["held"] * 3


# --- where held items go (and don't) ---------------------------------------------------------------

def test_held_items_stay_out_of_your_ok_and_telegram(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    rt.announce_new()  # notes where it's up to
    arrive(rt.db, "news", [item("A"), item("B"), item("C")])
    assert rt.brain.pending() == ([], []) and len(rt.brain.held()) == 3
    assert rt.announce_new() == 0 and bot.sent == []


def test_held_items_wait_on_their_own_tab_with_confirm_and_dismiss(settings):
    rt = Runtime(settings)
    arrive(rt.db, "news", [item("Read Together"), item("Darwin's Finches"), item("Love Languages")])
    client = TestClient(create_app(settings))
    page = client.get("/pocket?show=held").text
    assert "Held back · 3" in page and "Held back: a newsletter or schedule" in page and "Read Together" in page
    assert "/confirm" in page and "/dismiss" in page
    assert "3 held back as newsletters or mass mail" in client.get("/chat/calendar").text
    first = rt.brain.held()[0]
    client.post(f"/items/{first}/confirm")
    assert rt.db.get("item", first)["status"] == "confirmed" and len(rt.brain.held()) == 2


def test_held_items_that_pass_are_cleared_quietly_and_never_count_against_the_sender(settings):
    db = Runtime(settings).db
    for n in range(3):
        arrive(db, f"old{n}", [item(f"Talk {n}a", days=-3), item(f"Talk {n}b", days=-3), item(f"Talk {n}c", days=-3)])
    assert tidy(db, "Asia/Singapore", datetime.now(SG)) == ""  # nothing you saw, so nothing to tell you
    assert statuses(db, "old0") == ["dismissed"] * 3
    assert senders.records(db)["edm@experience.nlb.gov.sg"]["dismissed"] == 0
    assert senders.worth_skipping(db, Runtime(settings).rules, settings.user_emails) == []


def test_a_check_mentions_what_it_held_back(settings):
    rt = Runtime(settings)
    rt.check.report({"items": 1, "held": 4, "transactions": [], "processed": 3, "counts": {}}, auto=True)
    assert "1 new thing(s) for you (4 more held back as newsletters or mass mail" in rt.db.last_message("calendar")["text"]
