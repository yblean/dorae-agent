"""The monthly summary (Dorae-1, on the 1st) and searching for one thing ("when is my dentist appointment?")."""
import dataclasses
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from doraemon import income
from doraemon.assistant import month_name
from doraemon.email_parse import ParsedEmail
from doraemon.jobs import Runtime
from doraemon.schema import ActionItem, Extraction, Transaction

SG = ZoneInfo("Asia/Singapore")


class FakeBot:
    chat_id = "42"

    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)


@pytest.fixture
def rt(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone="Asia/Singapore", home_currency="SGD",
                            poll_minutes=0, google_token=str(tmp_path / "t.json"), google_credentials=str(tmp_path / "c.json"),
                            briefing_time="08:00")
    return Runtime(s, messenger=FakeBot())


def pay(rt, n, day, merchant, amount, category):
    at = datetime.combine(day, datetime.min.time(), SG).replace(hour=12)
    txn = Transaction(message_id="m", source="bank_alert", merchant=merchant, order_ref=None, purchased_at=at,
                      amount=Decimal(amount), currency="SGD", amount_home=None, category=category, is_refund=False)
    email = ParsedEmail(message_id="m", subject="Alert", sender="Bank <a@bank.example>", sent_at=at, body="")
    rt.db.save_result(f"p{n}", "t", email, Extraction(message_id="m", transactions=[txn]), "extracted")


def months(rt):
    this = rt.brain.this_month()
    return rt.brain.month_shift(this, -1), rt.brain.month_shift(this, -2)


def test_the_month_in_review_compares_and_explains(rt):
    last, before = months(rt)
    d = lambda month, day: date.fromisoformat(f"{month}-{day:02d}")
    pay(rt, 0, d(before, 3), "FairPrice", "100.00", "groceries")
    pay(rt, 1, d(before, 9), "Kopitiam", "50.00", "dining")
    pay(rt, 2, d(last, 4), "FairPrice", "120.00", "groceries")
    pay(rt, 3, d(last, 10), "Trip.com", "300.00", "travel")
    pay(rt, 4, d(last, 12), "Kopitiam", "30.00", "dining")
    rt.db.set_budget("total", Decimal("400"))
    income.add(rt.db, "Salary", "salary", Decimal("2000"), "SGD", d(before, 25), monthly=True)
    card = rt.brain.monthly_summary(last)
    text = card["text"]
    assert text.startswith(f"📅 {month_name(last)} in review: ")
    assert "you spent SGD 450.00 across 3 payments, SGD 300.00 (200%) more than" in text
    assert "That's 112% of your SGD 400.00 budget, SGD 50.00 over." in text
    assert "Most went to travel (SGD 300.00)." in text and "The biggest change: travel up SGD 300.00." in text
    assert "Biggest purchase: Trip.com, SGD 300.00" in text
    assert "From SGD 2,000.00 of income you kept SGD 1,550.00 (77%)." in text
    assert card["kind"] == "breakdown" and card["payload"]["label"].endswith("in review")


def test_it_is_posted_once_on_the_1st_and_sent_to_telegram(rt):
    last, _ = months(rt)
    pay(rt, 0, date.fromisoformat(f"{last}-05"), "FairPrice", "42.00", "groceries")
    first = datetime.combine(date.fromisoformat(f"{rt.brain.this_month()}-01"), datetime.min.time(), SG)
    assert not rt.monthly_tick(first.replace(hour=7))  # before the briefing time
    assert rt.monthly_tick(first.replace(hour=9))
    assert not rt.monthly_tick(first.replace(hour=10))
    assert not rt.monthly_tick(first + timedelta(days=1, hours=9))
    assert rt.db.last_message("money")["text"].startswith("📅 ") and rt.messenger.sent[-1].startswith("📅 ")


def test_asking_for_it_answers_instantly(rt):
    last, _ = months(rt)
    pay(rt, 0, date.fromisoformat(f"{last}-05"), "FairPrice", "42.00", "groceries")
    rt.brain.reply("money", "monthly summary")
    assert rt.brain.last_route == "summary" and "in review: you spent SGD 42.00" in rt.db.last_message("money")["text"]


# --- "when is my ...?" -----------------------------------------------------------------------------

def item(rt, n, title, days, hour=15):
    start = datetime.now(SG).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
    found = ActionItem(message_id="m", type="appointment", title=title, start_at=start, end_at=None, all_day=False,
                       timezone="Asia/Singapore", location="Tanjong Pagar", evidence_snippet="", confidence=0.9)
    email = ParsedEmail(message_id="m", subject="s", sender="a@b.c", sent_at=datetime.now(SG), body="")
    rt.db.save_result(f"i{n}", "t", email, Extraction(message_id="m", items=[found]), "extracted")


def test_when_is_my_dentist_appointment_finds_it_without_the_model(rt):
    item(rt, 0, "Dentist check-up", 6)
    item(rt, 1, "Haircut", 2)
    rt.brain.reply("calendar", "when is my dentist appointment?")
    msg = rt.db.last_message("calendar")
    assert rt.brain.last_route == "find" and msg["kind"] == "days"
    assert msg["text"].startswith("Dentist check-up: ") and ", 3pm, in 6 days · Tanjong Pagar." in msg["text"]


def test_nothing_found_says_so(rt):
    card = rt.brain.find_card("passport renewal")
    assert card["text"] == "I couldn't find “passport renewal” in your calendars or email in the next 4 months."


def test_a_refunded_purchase_isnt_the_biggest(rt):
    last, _ = months(rt)
    pay(rt, 0, date.fromisoformat(f"{last}-05"), "Trip.com", "278.05", "travel")
    pay(rt, 1, date.fromisoformat(f"{last}-07"), "FairPrice", "42.00", "groceries")
    at = datetime.combine(date.fromisoformat(f"{last}-20"), datetime.min.time(), SG)
    refund = Transaction(message_id="m", source="bank_alert", merchant="Trip.com", order_ref=None, purchased_at=at,
                         amount=Decimal("278.05"), currency="SGD", amount_home=None, category="travel", is_refund=True)
    rt.db.save_result("r", "t", ParsedEmail(message_id="m", subject="Refund", sender="Bank <a@bank.example>", sent_at=at,
                                            body=""), Extraction(message_id="m", transactions=[refund]), "extracted")
    assert "Biggest purchase: FairPrice, SGD 42.00" in rt.brain.monthly_summary(last)["text"]
