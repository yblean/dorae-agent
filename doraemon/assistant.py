"""Answers for the chat box, computed from your own data (no model call).

v0 understands a few questions: what's due soon, how a month's spending went,
and upcoming trips. Anything else gets a short list of what it can answer.
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon.config import Settings
from doraemon.db import Database, item_model, transaction_model
from doraemon.display import describe_when
from doraemon.ledger import build_ledger, spending_totals

TRAVEL = ("flight", "hotel", "other_travel")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]


def _today(settings: Settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


def _upcoming(db: Database, settings: Settings, days: int | None, types: tuple[str, ...] = ()) -> list:
    today = _today(settings)
    tz = ZoneInfo(settings.timezone)
    found = []
    for row in db.items(("proposed", "confirmed")):
        item = item_model(row)
        if item.start_at is None or (types and row["type"] not in types):
            continue
        day = item.start_at.astimezone(tz).date()
        if day >= today and (days is None or day <= today + timedelta(days=days)):
            found.append((item, row))
    return sorted(found, key=lambda pair: pair[0].start_at)


def last_full_month(settings: Settings) -> str:
    first = _today(settings).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def month_name(month: str) -> str:
    return _MONTHS[int(month[5:]) - 1].capitalize()


def due_soon(db: Database, settings: Settings) -> str:
    found = _upcoming(db, settings, days=7)
    if not found:
        return "Nothing due in the next 7 days. Enjoy the quiet week!"
    lines = [f"• {row['title']}: {describe_when(item, settings.timezone)}" for item, row in found[:8]]
    more = f"\n…and {len(found) - 8} more." if len(found) > 8 else ""
    waiting = sum(1 for _, row in found if row["status"] == "proposed")
    note = f"\n{waiting} of these still need your OK." if waiting else ""
    return f"Coming up in the next 7 days:\n" + "\n".join(lines) + more + note


def _month_totals(db: Database, settings: Settings, month: str, fx=None) -> tuple[dict[str, Decimal], int]:
    rows = [r for r in db.transactions() if r["status"] == "counted" and r["purchased_at"].startswith(month)]
    kept, _ = build_ledger([transaction_model(r) for r in rows])
    return spending_totals(kept, settings.home_currency, fx).by_category, len(kept)


def spending(db: Database, settings: Settings, month: str, fx=None) -> str:
    totals, n = _month_totals(db, settings, month, fx)
    home = settings.home_currency
    if not n:
        return f"I don't have any payments for {month_name(month)} yet."
    if not totals:
        return f"All {n} payments in {month_name(month)} were in other currencies, so I can't total them in {home} yet."
    total = sum(totals.values(), Decimal(0))
    top, top_amount = max(totals.items(), key=lambda kv: kv[1])
    text = f"You spent {home} {total} in {month_name(month)} across {n} payments. "
    text += f"{top.capitalize()} was the biggest at {home} {top_amount}."
    prev = (date.fromisoformat(month + "-01") - timedelta(days=1)).strftime("%Y-%m")
    prev_totals, prev_n = _month_totals(db, settings, prev, fx)
    if prev_n:
        diff = total - sum(prev_totals.values(), Decimal(0))
        text += f" That's {home} {abs(diff)} {'more' if diff > 0 else 'less'} than {month_name(prev)}."
    return text + " (Emailed receipts and bank alerts only; overseas payments converted to " + home + ".)"


def trips(db: Database, settings: Settings) -> str:
    found = _upcoming(db, settings, days=None, types=TRAVEL)
    if not found:
        return "No upcoming flights, hotels or trains in your email."
    return "Your upcoming travel:\n" + "\n".join(
        f"• {row['title']}: {describe_when(item, settings.timezone)}" for item, row in found[:10]
    )


def answer(question: str, db: Database, settings: Settings, fx=None) -> str:
    q = question.lower()
    for i, name in enumerate(_MONTHS):
        if re.search(rf"\b({name}|{name[:3]})\b", q):
            year = _today(settings).year if i + 1 <= _today(settings).month else _today(settings).year - 1
            return spending(db, settings, f"{year}-{i + 1:02d}", fx)
    if re.search(r"spen|money|budget|cost|paid|expens", q):
        month = _today(settings).strftime("%Y-%m") if "this month" in q else last_full_month(settings)
        return spending(db, settings, month, fx)
    if re.search(r"trip|travel|flight|hotel|train|holiday", q):
        return trips(db, settings)
    if re.search(r"due|week|bill|deadline|appoint|upcoming|soon|todo|to do", q):
        return due_soon(db, settings)
    return ("I can answer these for now: what's due this week, how a month's spending went "
            "(try \"How was September?\"), and your upcoming trips.")
