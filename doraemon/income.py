"""Income you add yourself: one-off amounts (a bonus, dividends) or monthly ones (salary).

A monthly entry counts once in every month from its first one until you stop it, on the same day
of the month (the last day, in shorter months). Totals are in your home currency, so they can be
set against spending (doraemon.ledger) for what you kept each month.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from doraemon.db import Database
from doraemon.networth import cents, month_end

CATEGORIES = {"salary": "Salary", "bonus": "Bonus", "freelance": "Freelance", "dividends": "Dividends",
              "interest": "Interest", "rental": "Rental", "gift": "Gift", "other": "Other"}


@dataclass
class Received:
    entry_id: int
    label: str
    category: str
    day: date
    amount: Decimal
    currency: str
    home: Decimal | None  # in your home currency; None without an exchange rate
    monthly: bool


def add(db: Database, label: str, category: str, amount: Decimal, currency: str, received_on: date,
        monthly: bool = False) -> int:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    if amount <= 0:
        raise ValueError("income must be more than zero")
    cur = db.conn.execute(
        "INSERT INTO income (label, category, amount, currency, received_on, monthly, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (label.strip()[:60] or CATEGORIES[category], category, str(cents(amount)), currency.upper(),
         received_on.isoformat(), int(monthly), datetime.now(timezone.utc).isoformat()))
    db.conn.commit()
    return cur.lastrowid


def entries(db: Database):
    return db.conn.execute("SELECT * FROM income ORDER BY monthly DESC, received_on DESC").fetchall()


def stop(db: Database, entry_id: int, last_day: date) -> None:
    """A monthly entry stops: its last time is on or before `last_day`."""
    db.conn.execute("UPDATE income SET ended_on = ? WHERE id = ?", (last_day.isoformat(), entry_id))
    db.conn.commit()


def delete(db: Database, entry_id: int) -> None:
    db.conn.execute("DELETE FROM income WHERE id = ?", (entry_id,))
    db.conn.commit()


def in_month(db: Database, month: str, home: str, fx=None, today: date | None = None) -> list[Received]:
    """Everything received in `month` (YYYY-MM). In the current month, monthly ones count once their day
    has come, so "this month so far" doesn't include a salary that hasn't arrived yet."""
    today = today or datetime.now(timezone.utc).date()
    first, last = date.fromisoformat(month + "-01"), min(month_end(month), today)
    found = []
    for e in db.conn.execute("SELECT * FROM income").fetchall():
        start = date.fromisoformat(e["received_on"])
        if e["monthly"]:
            day = first.replace(day=min(start.day, month_end(month).day))
            ended = date.fromisoformat(e["ended_on"]) if e["ended_on"] else None
            if day < start or day > last or (ended and day > ended):
                continue
        else:
            if not first <= start <= last:
                continue
            day = start
        amount = Decimal(e["amount"])
        if e["currency"] == home:
            in_home = amount
        else:
            rate = fx.rate(e["currency"], home, day, online=False) if fx else None
            in_home = cents(amount * rate) if rate is not None else None
        found.append(Received(e["id"], e["label"], e["category"], day, amount, e["currency"], in_home, bool(e["monthly"])))
    return sorted(found, key=lambda r: r.day)


def total(received: list[Received]) -> Decimal:
    return sum((r.home for r in received if r.home is not None), Decimal(0))

