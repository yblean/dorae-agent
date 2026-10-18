"""Doraemon with made-up data, to try it out (and for the README's screenshots).

    python -m doraemon.demo                # http://127.0.0.1:8100
    python -m doraemon.demo --port 8200

Everything is invented and dated around today: payments, bills, a trip, budgets, net worth and a
Google Calendar week. The demo's database is data/demo/demo.db, rebuilt at each start. Nothing real is
read or sent: no Gmail ("Check now" finds nothing), no Google Calendar, no Telegram, no brokerage and
no model, so the agents answer with their instant answers and fixed replies.
"""
import argparse
import dataclasses
import shutil
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from doraemon import income
from doraemon.config import Settings, in_project
from doraemon.db import item_model
from doraemon.email_parse import ParsedEmail
from doraemon.networth import NetWorth
from doraemon.reminders import plan
from doraemon.schema import ActionItem, Extraction, Transaction

TZ, HOME = "Asia/Singapore", "SGD"
FOLDER = "data/demo"


def demo_settings(folder: Path) -> Settings:
    """The demo's settings: its own database, and every outside connection off."""
    return dataclasses.replace(
        Settings(), timezone=TZ, home_currency=HOME, db_path=str(folder / "demo.db"), user_emails=("you@example.com",),
        google_credentials=str(folder / "no-google.json"), google_token=str(folder / "no-google-token.json"),
        telegram_token="", telegram_chat_id="", webull_app_key="", webull_app_secret="", chat_model="off",
        poll_minutes=0, briefing_time="06:00", briefing_telegram=False, start_ollama=False, fast_answers=True)


class DemoSchedule:
    """A made-up week of Google Calendar events, around today."""

    CALENDARS = {"Personal": "#039BE5", "Work": "#33B679", "Family": "#E67C73", "Holidays": "#8E24AA"}

    def __init__(self, today: date) -> None:
        monday = today - timedelta(days=today.weekday())
        self.events_list = []
        for week in (0, 1):
            for day in range(5):
                self.add(monday + timedelta(days=7 * week + day), "Team stand-up", "Work", "09:30", "09:45")
            self.add(monday + timedelta(days=7 * week + 1), "Gym", "Personal", "19:00", "20:00")
            self.add(monday + timedelta(days=7 * week + 3), "Gym", "Personal", "19:00", "20:00")
        self.add(monday + timedelta(days=2), "Project review", "Work", "14:00", "15:00", "Meeting room 3")
        self.add(monday + timedelta(days=4), "Dinner with Alex", "Personal", "19:30", "21:30", "Jumbo Seafood")
        self.add(monday + timedelta(days=5), "Mum's birthday", "Family")
        self.add(monday + timedelta(days=6), "Brunch with the family", "Family", "11:00", "13:00")
        self.add(monday + timedelta(days=9), "Quarterly planning", "Work", "10:00", "12:00")
        self.add(monday + timedelta(days=12), "Public holiday", "Holidays")
        self.add(monday + timedelta(days=24), "Concert: Coldplay", "Personal", "20:00", "23:00", "National Stadium")

    def add(self, day: date, title: str, calendar: str, start: str = "", end: str = "", location: str = "") -> None:
        self.events_list.append({"title": title, "first": day.isoformat(), "last": day.isoformat(), "start": start,
                                 "end": end, "calendar": calendar, "color": self.CALENDARS[calendar],
                                 "location": location, "link": "", "id": f"demo-{len(self.events_list)}"})

    def events(self, first: date, last: date) -> list[dict]:
        return [dict(e) for e in self.events_list if first.isoformat() <= e["first"] <= last.isoformat()]


class DemoCalendar:
    """Stands in for your Doraemon Google Calendar: Confirm 'adds' events without reaching Google."""

    def __init__(self) -> None:
        self.n = 0

    def upsert(self, event_id: str | None, body: dict) -> str:
        self.n += 1
        return event_id or f"demo-event-{self.n}"

    def delete(self, event_id: str) -> None:
        pass


def no_mail(settings, **_) -> dict:
    """'Check now' in the demo: there's no inbox to read."""
    return {"processed": 0, "items": 0, "transactions": [], "counts": {}, "stopped": ""}


# (days ago, merchant, amount, category[, currency, refund])
PAYMENTS = [
    (0, "FairPrice Xtra", "42.80", "groceries"), (0, "Kopitiam", "6.50", "dining"), (1, "Grab", "14.20", "transport"),
    (1, "Starbucks", "7.90", "dining"), (2, "Uniqlo", "59.90", "shopping"), (2, "BUS/MRT", "2.17", "transport"),
    (3, "Netflix", "19.98", "subscriptions"), (3, "Ya Kun Kaya Toast", "5.60", "dining"),
    (4, "Shopee", "36.40", "shopping"), (5, "Guardian", "18.75", "health"), (5, "Din Tai Fung", "48.20", "dining"),
    (6, "Golden Village", "27.00", "entertainment"), (7, "Cold Storage", "63.15", "groceries"),
    (8, "Grab", "22.80", "transport"), (9, "Spotify", "11.98", "subscriptions"), (10, "Anytime Fitness", "89.00", "memberships"),
    (11, "SP Group", "86.40", "utilities"), (12, "Kopitiam", "5.90", "dining"), (13, "Shopee", "24.10", "shopping"),
    (14, "Sheng Siong", "38.60", "groceries"), (15, "Hai Di Lao", "72.30", "dining"), (16, "Grab", "11.40", "transport"),
    (17, "Popular Bookstore", "21.90", "shopping"), (18, "FairPrice", "29.45", "groceries"),
    (20, "Uniqlo", "59.90", "shopping", HOME, True),  # a return
    (22, "Klook", "64.50", "travel"), (23, "Kopitiam", "6.20", "dining"),
    (25, "Trip.com", "312.40", "travel"), (27, "Grab", "18.60", "transport"), (29, "FairPrice", "51.20", "groceries"),
    (31, "Netflix", "19.98", "subscriptions"), (33, "Din Tai Fung", "39.80", "dining"), (35, "Shopee", "45.00", "shopping"),
    (38, "SP Group", "92.10", "utilities"), (40, "Anytime Fitness", "89.00", "memberships"), (42, "Cold Storage", "58.30", "groceries"),
    (45, "Grab", "25.40", "transport"), (48, "Golden Village", "24.00", "entertainment"), (50, "Uniqlo", "39.90", "shopping"),
    (53, "FairPrice", "47.60", "groceries"), (56, "Hai Di Lao", "68.00", "dining"), (60, "Spotify", "11.98", "subscriptions"),
    (62, "Shopee", "28.70", "shopping"), (65, "Grab", "16.30", "transport"), (68, "SP Group", "88.20", "utilities"),
    (70, "Kopitiam", "6.80", "dining"), (73, "Cold Storage", "44.90", "groceries"), (75, "Guardian", "22.40", "health"),
    (3, "STRIPE PAYMENTS SINGAPORE", "4.80", "other"), (6, "STRIPE PAYMENTS SINGAPORE", "5.20", "other"),
    (9, "STRIPE PAYMENTS SINGAPORE", "4.50", "other"),
]


def seed(rt, today: date) -> None:
    """Fill the demo's database: payments, things from 'email' to review, reminders, budgets, net worth, income."""
    db, tz = rt.db, ZoneInfo(TZ)
    noon = lambda d: datetime.combine(d, time(12, 30), tz)

    for n, (ago, merchant, amount, category, *rest) in enumerate(PAYMENTS):
        currency, refund = (rest + [HOME, False])[:2] if rest else (HOME, False)
        day = noon(today - timedelta(days=ago)) - timedelta(minutes=7 * n)
        txn = Transaction(message_id="m", source="bank_alert", merchant=merchant, order_ref=None, purchased_at=day,
                          amount=Decimal(amount), currency=currency, amount_home=None, category=category, is_refund=refund)
        email = ParsedEmail(message_id="m", subject="Transaction alert", sender="Demo Bank <alerts@bank.example>",
                            sent_at=day, body="")
        db.save_result(f"pay-{n}", f"t-{n}", email, Extraction(message_id="m", transactions=[txn]), "extracted")

    def item(n, sender, subject, kind, title, start, all_day=False, **extra):
        found = ActionItem(message_id="m", type=kind, title=title, start_at=start, end_at=extra.pop("end", None),
                           all_day=all_day, timezone=TZ, evidence_snippet=extra.pop("evidence", ""), confidence=0.9, **extra)
        email = ParsedEmail(message_id="m", subject=subject, sender=sender, sent_at=noon(today - timedelta(days=1)), body="")
        db.save_result(f"mail-{n}", f"thread-{n}", email, Extraction(message_id="m", items=[found]), "extracted")
        return db.conn.execute("SELECT MAX(id) FROM action_items").fetchone()[0]

    day = lambda n: datetime.combine(today + timedelta(days=n), time(0), tz)
    at = lambda n, h, m=0: datetime.combine(today + timedelta(days=n), time(h, m), tz)
    # waiting for your OK
    item(1, "SP Group <bills@spgroup.example>", "Your electricity bill is ready", "bill", "SP Group electricity bill",
         day(4), True, amount=Decimal("86.40"), currency=HOME, evidence="Amount due: S$86.40. Please pay by the due date.")
    item(2, "Smile Dental <hello@smiledental.example>", "Appointment confirmed", "appointment", "Dentist check-up",
         at(6, 15), location="Smile Dental, Tanjong Pagar", evidence="We look forward to seeing you at 3:00 PM.")
    item(3, "Singapore Airlines <noreply@sia.example>", "Your e-ticket", "flight", "SQ 638 Singapore to Tokyo",
         at(19, 8, 45), end=at(19, 16, 45), departs_from="Changi Airport (SIN)", booking_ref="K7Q2PX",
         evidence="Departure 08:45 Changi T3, arrival 16:45 Narita T1.")
    item(4, "Hotel Gracery <stay@gracery.example>", "Booking confirmation", "hotel", "Hotel Gracery Shinjuku",
         day(19), True, end=day(23), location="Shinjuku, Tokyo", booking_ref="HG-55821", amount=Decimal("612.00"),
         currency=HOME, evidence="Check-in from 14:00. 4 nights.")
    item(5, "Shopee <info@shopee.example>", "Your parcel is on its way", "delivery", "Shopee parcel (wireless earbuds)",
         day(1), True, evidence="Estimated delivery tomorrow.")
    item(6, "Sam & Jo <rsvp@wedding.example>", "You're invited!", "rsvp", "RSVP to Sam & Jo's wedding",
         day(9), True, evidence="Kindly reply by the date below.")
    # confirmed, with Telegram reminders set
    for n, (kind, title, start, all_day, amount) in enumerate([
            ("bill", "Credit card bill", day(3), True, Decimal("1240.55")),
            ("deadline", "Submit tax return", day(12), True, None)], 7):
        item_id = item(n, "Demo Bank <statements@bank.example>" if kind == "bill" else "IRAS <noreply@iras.example>",
                       "Your statement" if kind == "bill" else "Filing reminder", kind, title, start, all_day,
                       amount=amount, currency=HOME if amount else None)
        db.update("item", item_id, "confirm", status="confirmed")
        db.set_reminders(item_id, plan(item_model(db.get("item", item_id)), TZ, "1d", 2, datetime.now(tz))[0])

    for category, amount in (("total", "1200"), ("dining", "300"), ("shopping", "250"), ("transport", "120")):
        db.set_budget(category, Decimal(amount))

    nw = NetWorth(db, HOME)
    accounts = [("DBS Multiplier", "cash", 18200, 450), ("CPF", "cpf", 41300, 520), ("Stocks (manual)", "investment", 12400, 300),
                ("Car loan", "loan", 23800, -560)]
    month_ends, end = [], today.replace(day=1) - timedelta(days=1)
    for _ in range(5):  # the last 5 month ends, newest first
        month_ends.append(end)
        end = end.replace(day=1) - timedelta(days=1)
    for name, kind, now_value, step in accounts:  # growing a little each month (the loan shrinking)
        account = nw.add_account(name, kind, HOME)
        for months_ago, day_ in [(n + 1, d) for n, d in enumerate(month_ends)] + [(0, today)]:
            nw.set_balance(account, Decimal(now_value - step * months_ago), day_)
    income.add(db, "Salary", "salary", Decimal("4200"), HOME, (today.replace(day=1) - timedelta(days=80)).replace(day=25),
               monthly=True)

    # The morning briefing and Dorae-1's overview, then a few questions already asked, so the chats show what
    # they do (all instant: there's no model in the demo)
    rt.brain.ensure_overview("calendar", scheduled=True)
    rt.brain.ensure_overview("money")
    for agent, question in (("calendar", "what needs my OK?"), ("calendar", "what's my schedule like this week"),
                            ("money", "how much have I spent this month?"), ("money", "how am I doing on my budget")):
        rt.brain.reply(agent, question)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m doraemon.demo")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args()
    import uvicorn
    from doraemon.web import create_app

    folder = Path(in_project(FOLDER))
    shutil.rmtree(folder, ignore_errors=True)  # a fresh demo each time
    folder.mkdir(parents=True)
    settings = demo_settings(folder)
    today = datetime.now(ZoneInfo(TZ)).date()
    app = create_app(settings, schedule=DemoSchedule(today), calendar=DemoCalendar(), ingest=no_mail)
    seed(app.state.runtime, today)
    print(f"Doraemon demo with made-up data: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
