"""The two agents you chat with. Each only sees its own area.

Dorae-1 (money) answers about spending; Dorae-2 (calendar) about bills,
appointments, deadlines and trips, and holds the items waiting for your OK.
Each posts a short overview once a day; you ask for the details.

Answers are computed from your database (no model call yet): a reply is some
text plus, optionally, a card (breakdown bars, a payments table, item cards).
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon.assistant import last_full_month, month_name, spending as spending_text
from doraemon.config import Settings
from doraemon.db import Database, item_model, transaction_model
from doraemon.display import describe_when
from doraemon.ledger import build_ledger, spending_totals
from doraemon.rules import name_key
from doraemon.schema import Category


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    role: str
    color: str
    suggestions: tuple[str, ...]
    routines: tuple[tuple[str, str, str], ...]  # (name, when, status)


MONEY = Agent(
    "money", "Dorae-1", "Spending", "blue",
    ("Show this month's breakdown", "Where did I spend the most?", "How was {last_month}?", "Show my dining payments"),
    (("Check inbox for receipts", "With Dorae-2's check", "On"),
     ("Monthly summary", "1st of each month, 9:00", "Coming soon"),
     ("Overspend alerts", "When a budget is passed", "Coming soon")),
)
CALENDAR = Agent(
    "calendar", "Dorae-2", "Calendar & reminders", "pink",
    ("What needs my OK?", "What's due this week?", "My upcoming trips", "What already passed?"),
    (("Check inbox", "When you press Run now", "Run now"),
     ("Morning briefing", "Every day, 8:00", "Coming soon"),
     ("Due-soon reminders", "3 days and 1 day before", "Coming soon")),
)
AGENTS = {a.id: a for a in (MONEY, CALENDAR)}

_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]
_CATEGORY_WORDS = {
    "dining": "dining", "food": "dining", "eat": "dining", "restaurant": "dining", "meal": "dining",
    "grocer": "groceries", "supermarket": "groceries", "transport": "transport", "bus": "transport",
    "mrt": "transport", "taxi": "transport", "grab": "transport", "travel": "travel", "hotel": "travel",
    "flight": "travel", "shopping": "shopping", "shop": "shopping", "subscription": "subscriptions",
    "membership": "memberships", "gym": "memberships", "health": "health", "medical": "health",
    "utilit": "utilities", "bill": "utilities", "entertain": "entertainment", "other": "other",
}


def money(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount:,.2f}"


class Brain:
    def __init__(self, db: Database, settings: Settings, fx=None) -> None:
        self.db, self.settings, self.fx = db, settings, fx
        self.tz = ZoneInfo(settings.timezone)
        self.home = settings.home_currency

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def this_month(self) -> str:
        return self.today().strftime("%Y-%m")

    def _month_in(self, q: str) -> str | None:
        if "last month" in q:
            return last_full_month(self.settings)
        if "this month" in q:
            return self.this_month()
        for i, name in enumerate(_MONTHS):
            if re.search(rf"\b({name}|{name[:3]})\b", q):
                year = self.today().year if i + 1 <= self.today().month else self.today().year - 1
                return f"{year}-{i + 1:02d}"
        return None

    # --- money ---------------------------------------------------------------

    def ledger(self, month: str):
        rows = [r for r in self.db.transactions() if r["status"] == "counted" and r["purchased_at"].startswith(month)]
        kept, _ = build_ledger([transaction_model(r) for r in rows])
        return kept

    def breakdown(self, month: str) -> dict:
        kept = self.ledger(month)
        sums = spending_totals(kept, self.home, self.fx)
        rows = sorted(sums.by_category.items(), key=lambda kv: -kv[1])
        return {"text": "", "kind": "breakdown", "payload": {
            "month": month, "label": month_name(month), "currency": self.home, "count": len(kept),
            "total": str(sums.total), "rows": [[c, str(a)] for c, a in rows],
            "unconverted": len(sums.unconverted),
            "converted": sum(1 for t in kept if t.currency != self.home and id(t) in sums.home_amounts),
        }}

    def money_overview(self) -> list[dict]:
        month, last = self.this_month(), last_full_month(self.settings)
        card = self.breakdown(month)
        p = card["payload"]
        if p["count"]:
            top = p["rows"][0] if p["rows"] else None
            card["text"] = (f"Here's {p['label']} so far: {money(Decimal(p['total']), self.home)} across "
                            f"{p['count']} payments" + (f", mostly {top[0]}." if top else "."))
        else:
            card = self.breakdown(last)
            card["text"] = f"No payments in {month_name(month)} yet. Here's {month_name(last)}:"
        last_total = spending_totals(self.ledger(last), self.home, self.fx).total
        extra = f" {month_name(last)} came to {money(last_total, self.home)}." if card["payload"]["month"] != last else ""
        card["text"] += extra + " Ask me for any month, category or merchant."
        return [card]

    def payments(self, month: str, category: str | None = None, limit: int = 40) -> dict:
        kept = [t for t in self.ledger(month) if category is None or t.category.value == category]
        kept.sort(key=lambda t: t.purchased_at, reverse=True)
        what = f"{category} payments" if category else "payments"
        if not kept:
            return {"text": f"No {what} in {month_name(month)}.", "kind": "text", "payload": None}
        sums = spending_totals(kept, self.home, self.fx)
        text = f"{len(kept)} {what} in {month_name(month)}, {money(sums.total, self.home)} in total."
        if len(kept) > limit:
            text += f" Showing the latest {limit}."
        return {"text": text + " Change a category and I'll remember it for that merchant.",
                "kind": "payments", "payload": {"ids": [int(t.message_id) for t in kept[:limit]]}}

    def top_merchants(self, month: str) -> dict:
        kept = self.ledger(month)
        sums = spending_totals(kept, self.home, self.fx)
        by_merchant: dict[str, list] = {}
        for t in kept:
            if id(t) not in sums.home_amounts:
                continue
            key = name_key(t.merchant) or t.merchant.lower()
            entry = by_merchant.setdefault(key, [t.merchant, Decimal(0), 0])
            entry[1] += -sums.home_amounts[id(t)] if t.is_refund else sums.home_amounts[id(t)]
            entry[2] += 1
        top = sorted(by_merchant.values(), key=lambda e: -e[1])[:5]
        if not top:
            return {"text": f"No payments in {month_name(month)}.", "kind": "text", "payload": None}
        lines = [f"{i}. {name}: {money(total, self.home)}" + (f" ({n} payments)" if n > 1 else "")
                 for i, (name, total, n) in enumerate(top, 1)]
        card = self.breakdown(month)
        card["text"] = f"Where your money went in {month_name(month)}:\n" + "\n".join(lines)
        return card

    def money_reply(self, q: str) -> list[dict]:
        q = q.lower()
        month = self._month_in(q)
        category = next((c for word, c in _CATEGORY_WORDS.items() if re.search(rf"\b{word}", q)), None)
        if category and category in {c.value for c in Category}:
            return [self.payments(month or self.this_month(), category)]
        if re.search(r"most|biggest|top|where", q):
            return [self.top_merchants(month or self.this_month())]
        if re.search(r"breakdown|categor|split", q):
            card = self.breakdown(month or self.this_month())
            card["text"] = f"{month_name(card['payload']['month'])} by category:"
            return [card]
        if re.search(r"list|payments|transactions|all|show", q):
            return [self.payments(month or self.this_month())]
        if month or re.search(r"spen|how much|total|cost|compare|month|how was", q):
            target = month or self.this_month()
            card = self.breakdown(target)
            card["text"] = spending_text(self.db, self.settings, target, self.fx)
            return [card]
        return [{"text": "I look after your spending. Try: \"Show this month's breakdown\", \"Where did I spend "
                         "the most?\", \"Show my dining payments\" or \"How was September?\"",
                 "kind": "text", "payload": None}]

    # --- calendar ------------------------------------------------------------

    def _dated(self, row) -> date | None:
        item = item_model(row)
        return item.start_at.astimezone(self.tz).date() if item.start_at else None

    def pending(self) -> tuple[list[int], list[int]]:
        """Items waiting for your OK: (coming up, soonest first; already passed, latest first)."""
        rows = self.db.items(("proposed",))
        today = self.today()
        upcoming = sorted((r for r in rows if (d := self._dated(r)) is None or d >= today),
                          key=lambda r: (r["start_at"] is None, r["start_at"] or ""))
        past = sorted((r for r in rows if (d := self._dated(r)) is not None and d < today),
                      key=lambda r: r["start_at"], reverse=True)
        return [r["id"] for r in upcoming], [r["id"] for r in past]

    def agenda(self, days: int, types: tuple[str, ...] = ()) -> list[int]:
        today = self.today()
        rows = [r for r in self.db.items(("proposed", "confirmed"))
                if (d := self._dated(r)) is not None and today <= d <= today + timedelta(days=days)
                and (not types or r["type"] in types)]
        return [r["id"] for r in sorted(rows, key=lambda r: r["start_at"])]

    def calendar_overview(self) -> list[dict]:
        upcoming, past = self.pending()
        week = self.agenda(7)
        hour = datetime.now(self.tz).hour
        hello = "Good morning!" if hour < 12 else "Good afternoon!" if hour < 18 else "Good evening!"
        if upcoming:
            text = f"{hello} {len(upcoming)} thing{'s' if len(upcoming) != 1 else ''} from your email need your OK."
            if len(upcoming) > 5:
                text += " Here are the soonest; ask \"What needs my OK?\" for all of them."
        else:
            text = f"{hello} Nothing needs your OK right now."
        text += f" {len(week)} thing{'s' if len(week) != 1 else ''} in the next 7 days."
        if past:
            text += f" ({len(past)} already passed: ask \"What already passed?\")"
        msgs = [{"text": text, "kind": "items" if upcoming else "text",
                 "payload": {"ids": upcoming[:5], "more": max(0, len(upcoming) - 5)} if upcoming else None}]
        return msgs

    def calendar_reply(self, q: str) -> list[dict]:
        q = q.lower()
        upcoming, past = self.pending()
        if re.search(r"past|passed|missed|old|overdue", q):
            if not past:
                return [{"text": "Nothing that's already passed is waiting for you.", "kind": "text", "payload": None}]
            return [{"text": f"{len(past)} item{'s' if len(past) != 1 else ''} already passed. Confirm the ones "
                             "you still want on record, or dismiss them.",
                     "kind": "items", "payload": {"ids": past[:20], "more": max(0, len(past) - 20)}}]
        if re.search(r"trip|travel|flight|hotel|train|holiday", q):
            ids = self.agenda(365, ("flight", "hotel", "other_travel"))
            if not ids:
                return [{"text": "No upcoming flights, hotels or trains in your email.", "kind": "text", "payload": None}]
            return [{"text": f"Your upcoming travel ({len(ids)}):", "kind": "agenda", "payload": {"ids": ids}}]
        if re.search(r"today|tomorrow", q):
            days = 0 if "today" in q else 1
            target = self.today() + timedelta(days=days)
            ids = [i for i in self.agenda(days) if self._dated(self.db.get("item", i)) == target]
            label = "today" if days == 0 else "tomorrow"
            if not ids:
                return [{"text": f"Nothing on {label}.", "kind": "text", "payload": None}]
            return [{"text": f"On {label}:", "kind": "agenda", "payload": {"ids": ids}}]
        if re.search(r"due|week|soon|upcoming|bill|deadline|coming", q):
            ids = self.agenda(7)
            if not ids:
                return [{"text": "Nothing due in the next 7 days. Enjoy the quiet week!", "kind": "text", "payload": None}]
            waiting = sum(1 for i in ids if self.db.get("item", i)["status"] == "proposed")
            text = f"Coming up in the next 7 days ({len(ids)}):"
            if waiting:
                text += f" {waiting} still need your OK."
            return [{"text": text, "kind": "agenda", "payload": {"ids": ids}}]
        if re.search(r"ok|pending|review|need|confirm|waiting|what.*found|all", q) or not q.strip():
            if not upcoming:
                return [{"text": "Nothing needs your OK right now. 🎉", "kind": "text", "payload": None}]
            return [{"text": f"{len(upcoming)} thing{'s' if len(upcoming) != 1 else ''} waiting for your OK, soonest first:",
                     "kind": "items", "payload": {"ids": upcoming[:20], "more": max(0, len(upcoming) - 20)}}]
        return [{"text": "I look after your calendar. Try: \"What needs my OK?\", \"What's due this week?\", "
                         "\"My upcoming trips\" or \"What's on tomorrow?\"", "kind": "text", "payload": None}]

    # --- shared --------------------------------------------------------------

    def ensure_overview(self, agent: str) -> None:
        """Post today's overview once a day, so opening the chat shows where things stand."""
        key = f"overview:{agent}"
        today = self.today().isoformat()
        if self.db.get_setting(key) == today:
            return
        for msg in (self.money_overview() if agent == "money" else self.calendar_overview()):
            self.db.add_message(agent, "agent", msg["text"], msg["kind"], msg["payload"])
        self.db.set_setting(key, today)

    def reply(self, agent: str, question: str) -> list[int]:
        """Store your question and the agent's answer; returns the new message ids."""
        ids = [self.db.add_message(agent, "user", question)]
        answers = self.money_reply(question) if agent == "money" else self.calendar_reply(question)
        for msg in answers:
            ids.append(self.db.add_message(agent, "agent", msg["text"], msg["kind"], msg["payload"]))
        return ids
