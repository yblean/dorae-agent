"""Skipping senders you never want, and approving new suggestions from Telegram."""
import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon import senders
from doraemon.email_parse import ParsedEmail
from doraemon.jobs import Runtime
from doraemon.schema import ActionItem, Extraction
from doraemon.tidy import TWIN
from doraemon.web import create_app

SG = ZoneInfo("Asia/Singapore")
NLB = "National Library Board <edm@experience.nlb.gov.sg>"


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


def found(db, n, sender=NLB, title=None, days=5, kind="appointment", status="proposed"):
    start = datetime.now(SG).replace(hour=14, minute=0, second=0, microsecond=0) + timedelta(days=days)
    item = ActionItem(message_id="m", type=kind, title=title or f"Library workshop {n}", start_at=start, end_at=None,
                      all_day=False, timezone="Asia/Singapore", evidence_snippet="Join us!", confidence=0.9)
    email = ParsedEmail(message_id="m", subject="s", sender=sender, sent_at=datetime.now(SG), body="")
    db.save_result(f"g{n}-{sender[:5]}", "t", email, Extraction(message_id="m", items=[item]), "extracted")
    item_id = db.conn.execute("SELECT MAX(id) FROM action_items").fetchone()[0]
    if status != "proposed":
        db.update("item", item_id, status, status=status)
    return item_id


# --- who to offer ---------------------------------------------------------------------------

def test_three_dismissed_and_none_confirmed_earns_an_offer(settings):
    rt = Runtime(settings)
    for n in range(2):
        found(rt.db, n, status="dismissed")
    assert senders.worth_skipping(rt.db, rt.rules, settings.user_emails) == []
    found(rt.db, 2, status="dismissed")
    (offer,) = senders.worth_skipping(rt.db, rt.rules, settings.user_emails)
    assert (offer["address"], offer["name"], offer["dismissed"]) == ("edm@experience.nlb.gov.sg", "National Library Board", 3)


def test_no_offer_for_a_sender_you_confirmed_once_for_yourself_or_for_repeats(settings):
    rt = Runtime(settings)
    for n in range(3):
        found(rt.db, n, sender="SIT <sit@sit.example>", status="dismissed")
        found(rt.db, n, sender="Me <me@example.com>", status="dismissed")  # your own forwards
    found(rt.db, 9, sender="SIT <sit@sit.example>", status="confirmed")
    twins = [found(rt.db, n + 20, sender="Shop <shop@shop.example>") for n in range(3)]
    for item_id in twins:
        rt.db.update("item", item_id, TWIN, status="dismissed")  # only repeats of another item
    assert senders.worth_skipping(rt.db, rt.rules, settings.user_emails) == []


def test_skip_adds_the_rule_and_clears_what_is_waiting(settings):
    rt = Runtime(settings)
    for n in range(3):
        found(rt.db, n, status="dismissed")
    waiting = found(rt.db, 5)
    text, n = senders.skip(rt.db, rt.rules, NLB, "test")
    assert n == 1 and rt.db.get("item", waiting)["status"] == "dismissed"
    assert text.startswith("Done: I won't read emails from National Library Board (edm@experience.nlb.gov.sg)")
    assert rt.rules.ignored_by(NLB) and senders.worth_skipping(rt.db, rt.rules) == []


# --- where it's offered ------------------------------------------------------------------------

def test_the_third_dismiss_in_the_app_offers_to_skip_the_sender(settings):
    client = TestClient(create_app(settings), headers={"x-requested-with": "fetch"})
    db = client.app.state.db
    ids = [found(db, n) for n in range(4)]
    assert "Skip their emails" not in client.post(f"/items/{ids[0]}/dismiss").json()["html"]
    client.post(f"/items/{ids[1]}/dismiss")
    html = client.post(f"/items/{ids[2]}/dismiss").json()["html"]
    assert "You&#39;ve dismissed all 3 things I found in emails from National Library Board" in html
    assert 'action="/senders/skip"' in html and "edm@experience.nlb.gov.sg" in html
    client.post(f"/items/{ids[3]}/dismiss")  # asked once: not again
    assert sum(1 for m in db.messages("calendar") if m["kind"] == "skip") == 1
    html = client.post("/senders/skip", data={"address": "edm@experience.nlb.gov.sg"}).json()["html"]
    assert "I won&#39;t read emails from" in html and client.app.state.rules.ignored_by(NLB)


def test_the_history_is_offered_once_in_the_chat_and_on_telegram(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    for n in range(4):
        found(rt.db, n, status="dismissed")
    for n in range(3):
        found(rt.db, n, sender="Fantasy PL <fantasy@pl.example>", status="dismissed")
    offers = rt.offer_skips()
    assert [o["dismissed"] for o in offers] == [4, 3]
    msg = rt.db.last_message("calendar")
    assert msg["kind"] == "skip" and msg["text"].startswith("You've dismissed everything I found in emails from 2 senders")
    text, buttons = bot.sent[-1]
    assert [row[0][0] for row in buttons] == ["🚫 Skip National Library Board", "🚫 Skip Fantasy PL"]
    assert rt.offer_skips() == []  # once
    assert rt.button(buttons[0][0][1]).startswith("Done: I won't read emails from National Library Board")


def test_a_dismiss_on_telegram_can_offer_too(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    ids = [found(rt.db, n) for n in range(3)]
    for item_id in ids[:2]:
        rt.button(f"drop:{item_id}")
    assert bot.sent == []
    assert rt.button(f"drop:{ids[2]}") == "✖ Cancelled."
    text, buttons = bot.sent[-1]
    assert "dismissed all 3" in text and buttons == [[("🚫 Skip emails from National Library Board", f"skip:{ids[2]}")]]


# --- new suggestions on Telegram -----------------------------------------------------------------

def test_new_suggestions_arrive_on_telegram_with_buttons(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    found(rt.db, 0)  # waiting from before: not sent
    assert rt.announce_new() == 0 and bot.sent == []
    bill = found(rt.db, 1, sender="SP Group <bills@sp.example>", title="SP electricity bill", kind="bill")
    dentist = found(rt.db, 2, sender="Smile Dental <hi@dental.example>", title="Dentist check-up")
    assert rt.announce_new() == 2
    (bill_text, bill_buttons), (dentist_text, dentist_buttons) = bot.sent
    assert bill_text.startswith("📬 New from your email: SP electricity bill\nBill · ") and "From SP Group" in bill_text
    assert bill_buttons == [[("🔔 Remind me", f"make:{bill}"), ("✖ Dismiss", f"drop:{bill}")]]
    assert dentist_buttons[0][0] == ("✅ Confirm", f"make:{dentist}")
    assert rt.announce_new() == 0  # each once
    assert rt.button(f"make:{bill}").startswith("✅ Created. I'll remind you ")
    assert rt.db.get("item", bill)["status"] == "confirmed" and rt.db.reminders(bill)


def test_new_suggestions_can_stay_in_the_app(settings):
    bot = FakeBot()
    rt = Runtime(dataclasses.replace(settings, telegram_new_items=False), messenger=bot)
    rt.announce_new()
    found(rt.db, 1)
    assert rt.announce_new() == 0 and bot.sent == []


def test_the_first_find_after_starting_is_sent_too(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    found(rt.db, 0)  # waiting from before
    rt.start_jobs()
    rt.stop_jobs()
    found(rt.db, 1, title="Dentist check-up")
    assert rt.announce_new() == 1 and bot.sent[0][0].startswith("📬 New from your email: Dentist check-up")
