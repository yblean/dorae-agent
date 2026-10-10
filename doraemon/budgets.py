"""Monthly budgets: how each one is going, what would keep it on track, and alerts.

A budget is per calendar month, for all your spending (TOTAL) or one category.
Everything here is plain arithmetic on the ledger, so the numbers can be tested;
the chat model only puts them into words.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from doraemon.schema import Category

TOTAL = "total"
ALERT_LEVELS = (80, 100)  # percent of a budget used that earns a message in Dorae-1's chat
CATEGORIES = [c.value for c in Category]
_CATEGORY_WORDS = {"food": "dining", "eating out": "dining", "restaurants": "dining", "grocery": "groceries",
                   "subscription": "subscriptions", "membership": "memberships", "bills": "utilities",
                   "fun": "entertainment", "medical": "health", "transportation": "transport"}


def days_in(month: str) -> int:
    first = date.fromisoformat(month + "-01")
    return ((first + timedelta(days=32)).replace(day=1) - first).days


def cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), ROUND_HALF_UP)


@dataclass
class Status:
    category: str      # TOTAL or a Category value
    limit: Decimal
    spent: Decimal
    day: int           # days of the month gone, today included
    days: int          # days in the month

    @property
    def left(self) -> Decimal:
        return self.limit - self.spent

    @property
    def used(self) -> int:
        """Percent of the budget used, not capped (120 means 20% over)."""
        return int(self.spent / self.limit * 100) if self.limit else 0

    @property
    def pace(self) -> int:
        """Percent of the month gone: spending evenly, `used` would be about this."""
        return int(self.day / self.days * 100)

    @property
    def projected(self) -> Decimal:
        """Month-end spending if the rest of the month goes like the days so far."""
        return cents(self.spent / self.day * self.days) if self.day else self.spent

    @property
    def days_left(self) -> int:
        return self.days - self.day

    @property
    def per_day(self) -> Decimal | None:
        """What you can spend a day for the rest of the month and stay within; None once it's over or used up."""
        if self.days_left <= 0 or self.left <= 0:
            return None
        return cents(self.left / self.days_left)

    @property
    def state(self) -> str:
        if self.spent > self.limit:
            return "over"
        if self.days_left > 0 and self.projected > self.limit:
            return "at_risk"
        return "on_track"

    @property
    def label(self) -> str:
        return "Overall" if self.category == TOTAL else self.category.capitalize()


def statuses(budgets: dict[str, Decimal], by_category: dict[str, Decimal], total: Decimal,
             month: str, today: date) -> list[Status]:
    """Each budget against the month's spending, the overall one first."""
    days = days_in(month)
    this_month = today.strftime("%Y-%m")
    day = today.day if month == this_month else (days if month < this_month else 0)
    order = [TOTAL, *CATEGORIES]
    return [Status(c, budgets[c], by_category.get(c, Decimal(0)) if c != TOTAL else total, day, days)
            for c in sorted(budgets, key=lambda c: order.index(c) if c in order else 99)]


def describe(s: Status, home: str) -> str:
    """One line on a budget, e.g. 'Dining: SGD 180.00 of SGD 200.00 used (90%), 21 days left.'"""
    text = f"{s.label}: {home} {s.spent:,.2f} of {home} {s.limit:,.2f} used ({s.used}%)"
    if s.state == "over":
        return text + f", {home} {-s.left:,.2f} over."
    if s.days_left <= 0:
        return text + ", month done."
    text += f", {s.days_left} day{'s' if s.days_left != 1 else ''} left"
    if s.state == "at_risk":
        return text + (f". At this pace it reaches {home} {s.projected:,.2f}: keep to {home} {s.per_day:,.2f} "
                       "a day to stay within.")
    return text + "."


def parse_request(text: str) -> tuple[str, Decimal] | None:
    """'set my dining budget to $300' -> ('dining', 300). Only clear requests with an amount count."""
    q = text.lower()
    if not re.search(r"\bbudget", q) or not re.search(r"\b(set|make|change|update|put|create|give me|i want)\b", q):
        return None
    amount = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)\s*(k\b)?", q.replace("$", " "))
    if not amount:
        return None
    value = Decimal(amount.group(1).replace(",", "")) * (1000 if amount.group(2) else 1)
    category = next((c for c in CATEGORIES if re.search(rf"\b{c}\b|\b{c.rstrip('s')}\b", q)), None)
    category = category or next((c for word, c in _CATEGORY_WORDS.items() if word in q), None)
    return (category or TOTAL), cents(value)


def new_alerts(stats: list[Status], seen: dict[str, int]) -> list[tuple[Status, int]]:
    """Budgets that just passed an alert level, with that level. `seen`: highest level already sent per category."""
    found = []
    for s in stats:
        level = max((lv for lv in ALERT_LEVELS if s.used >= lv), default=0)
        if level > seen.get(s.category, 0):
            found.append((s, level))
    return found
