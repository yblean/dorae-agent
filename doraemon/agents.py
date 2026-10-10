"""The two agents you chat with. Each only sees its own area.

Dorae-1 (money) answers about spending; Dorae-2 (calendar) about bills,
appointments, deadlines and trips, and holds the items waiting for your OK.
Each posts a short overview once a day; you ask for the details.

A reply is some text plus, optionally, a card (breakdown bars, a payments
table, item cards, your week from Google Calendar). The local model answers
questions by calling read-only tools (doraemon.chat); the keyword answers here
are the fallback when it isn't running.
"""
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from doraemon.assistant import last_full_month, month_name, spending as spending_text
from doraemon.calendar_sync import ALL_DAY_TYPES, Schedule
from doraemon.config import Settings
from doraemon.db import Database, from_chat, item_model, transaction_model
from doraemon.display import describe_when
from doraemon.ledger import build_ledger, spending_totals
from doraemon.rules import name_key
from doraemon.schema import Category

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    role: str
    color: str
    suggestions: tuple[str, ...]
    routines: tuple[tuple[str, str, str], ...]  # (name, when, status)


MONEY = Agent(
    "money", "Dorae-1", "Spending", "#3B82F6",
    ("What's my latest purchase?", "Show this month's breakdown", "Where did I spend the most?", "How was {last_month}?"),
    (("Check inbox for receipts", "With Dorae-2's check", "On"),
     ("Monthly summary", "1st of each month, 9:00", "Coming soon"),
     ("Overspend alerts", "When a budget is passed", "Coming soon")),
)
CALENDAR = Agent(
    "calendar", "Dorae-2", "Calendar & reminders", "#EC4899",
    ("What needs my OK?", "Show my schedule this week", "What's due this week?", "My upcoming trips"),
    (("Check inbox", "Every 15 minutes", "Run now"),
     ("Morning briefing", "Every day, 8:00", "Coming soon"),
     ("Due-soon reminders", "3 days and 1 day before", "Coming soon")),
)
AGENTS = {a.id: a for a in (MONEY, CALENDAR)}
# The swatches offered when you customize an agent
COLORS = ["#8B5E3C", "#EF4444", "#F97316", "#F59E0B", "#22C55E", "#14B8A6", "#3B82F6", "#8B5CF6", "#EC4899", "#6B7280"]


def styled(agent: Agent, db: Database) -> Agent:
    """The agent with the name and colour you picked, if you customized it."""
    name = db.get_setting(f"agent:{agent.id}:name") or agent.name
    color = db.get_setting(f"agent:{agent.id}:color") or agent.color
    return replace(agent, name=name, color=color)


def shades(hex_color: str) -> list[str]:
    """Five tones for the 3D avatar: highlight, light, base, shade, deep shadow."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))

    def mix(target: int, amount: float) -> str:
        return "#" + "".join(f"{round(c + (target - c) * amount):02X}" for c in (r, g, b))
    return [mix(255, .55), mix(255, .18), hex_color, mix(0, .3), mix(0, .65)]

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
    def __init__(self, db: Database, settings: Settings, fx=None,
                 schedule: Callable[[], Schedule | None] | None = None) -> None:
        self.db, self.settings, self.fx = db, settings, fx
        self.schedule = schedule  # your Google calendars, or None until reading them is approved
        self.chat = None  # doraemon.chat.AgentChat when a chat model is set
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

    PERIODS = ("today", "yesterday", "this_week", "last_week", "last_7_days", "this_month", "last_month")

    def month_range(self, month: str) -> tuple[date, date]:
        first = date.fromisoformat(month + "-01")
        return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    def period_range(self, name: str | None) -> tuple[date, date, str] | None:
        """A named period as (first day, last day, label) in your timezone. The app does the date maths, not the model."""
        today = self.today()
        monday = today - timedelta(days=today.weekday())
        day = lambda d: f"{d:%a} {d.day} {d:%b}"
        if name == "today":
            return today, today, f"today ({day(today)})"
        if name == "yesterday":
            y = today - timedelta(days=1)
            return y, y, f"yesterday ({day(y)})"
        if name == "this_week":
            return monday, today, f"this week ({day(monday)} to {day(today)})"
        if name == "last_week":
            first, last = monday - timedelta(days=7), monday - timedelta(days=1)
            return first, last, f"last week ({day(first)} to {day(last)})"
        if name == "last_7_days":
            return today - timedelta(days=6), today, f"the last 7 days ({day(today - timedelta(days=6))} to {day(today)})"
        if name == "this_month":
            return today.replace(day=1), today, f"{month_name(self.this_month())} so far"
        if name == "last_month":
            month = last_full_month(self.settings)
            return (*self.month_range(month), month_name(month))
        return None

    def ledger(self, month: str):
        rows = [r for r in self.db.transactions() if r["status"] == "counted" and r["purchased_at"].startswith(month)]
        kept, _ = build_ledger([transaction_model(r) for r in rows])
        return kept

    def ledger_between(self, first: date | None, last: date | None):
        """Counted payments from the first to the last day (both included, your timezone), duplicates dropped."""
        day = lambda r: datetime.fromisoformat(r["purchased_at"]).astimezone(self.tz).date()
        rows = [r for r in self.db.transactions() if r["status"] == "counted"
                and (first is None or day(r) >= first) and (last is None or day(r) <= last)]
        kept, _ = build_ledger([transaction_model(r) for r in rows])
        return kept

    def breakdown(self, month: str) -> dict:
        return self.summary(self.ledger(month), month_name(month), month)

    def summary(self, kept, label: str, month: str | None = None) -> dict:
        """A breakdown card for any set of payments: total and split by category."""
        sums = spending_totals(kept, self.home, self.fx)
        rows = sorted(sums.by_category.items(), key=lambda kv: -kv[1])
        return {"text": "", "kind": "breakdown", "payload": {
            "month": month, "label": label, "currency": self.home, "count": len(kept),
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
        return self.top_merchants_of(self.ledger(month), month_name(month), month)

    def top_merchants_of(self, kept, label: str, month: str | None = None) -> dict:
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
            return {"text": f"No payments in {label}.", "kind": "text", "payload": None}
        lines = [f"{i}. {name}: {money(total, self.home)}" + (f" ({n} payments)" if n > 1 else "")
                 for i, (name, total, n) in enumerate(top, 1)]
        card = self.summary(kept, label, month)
        card["text"] = f"Where your money went in {label}:\n" + "\n".join(lines)
        return card

    def budget_check(self, q: str) -> dict:
        """Budgets can't be saved yet; compare the amount you mention with this month instead."""
        text = "I can't save budgets yet. That's coming together with overspend alerts."
        found = re.search(r"(\d[\d,]*(?:\.\d+)?)", q)
        if not found:
            return {"text": text + " Tell me an amount, like \"a budget of $300\", and I'll check this month against it.",
                    "kind": "text", "payload": None}
        limit = Decimal(found.group(1).replace(",", ""))
        month = self.this_month()
        spent = spending_totals(self.ledger(month), self.home, self.fx).total
        day = self.today().day
        next_month = (date.fromisoformat(month + "-01") + timedelta(days=32)).replace(day=1)
        days_in_month = (next_month - timedelta(days=1)).day
        if spent > limit:
            status = f"you're already {money(spent - limit, self.home)} over"
        else:
            on_track = limit * Decimal(day) / Decimal(days_in_month)
            status = (f"{money(limit - spent, self.home)} left for the rest of the month"
                      + (", and ahead of a steady pace" if spent > on_track else ", on track so far"))
        card = self.breakdown(month)
        card["text"] = (f"{text} For now, here's {month_name(month)} against {money(limit, self.home)}: "
                        f"{money(spent, self.home)} spent by day {day}, so {status}.")
        return card

    def money_reply(self, q: str) -> list[dict]:
        q = q.lower()
        if re.search(r"budget|overspen|spending limit|\bcap\b", q):
            return [self.budget_check(q)]
        named = next((p for p in ("today", "yesterday", "this week", "last week") if re.search(rf"\b{p}\b", q)), None)
        if named:
            first, last, label = self.period_range(named.replace(" ", "_"))
            kept = self.ledger_between(first, last)
            card = self.summary(kept, label[0].upper() + label[1:])
            total = Decimal(card["payload"]["total"])
            card["text"] = (f"You spent {money(total, self.home)} {label} across {len(kept)} payment"
                            f"{'s' if len(kept) != 1 else ''}." if kept else f"No payments {label}.")
            return [card]
        month = self._month_in(q)
        category = next((c for word, c in _CATEGORY_WORDS.items() if re.search(rf"\b{word}", q)), None)
        if category and category in {c.value for c in Category}:
            return [self.payments(month or self.this_month(), category)]
        if re.search(r"latest|last (purchase|payment)|recent", q):
            return [self.payments(month or self.this_month(), limit=5)]
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

    def week(self, q: str) -> dict:
        """Your week (Monday to Sunday) from all your Google calendars, plus email items not in it yet."""
        monday = self.today() - timedelta(days=self.today().weekday())
        if "next week" in q:
            monday += timedelta(days=7)
        sunday = monday + timedelta(days=6)
        source = self.schedule() if self.schedule else None
        if source is None:
            return {"text": "To show your week I need to read your Google calendars (read-only). Run "
                            "python -m doraemon.calendar_sync connect once and approve.", "kind": "text", "payload": None}
        try:
            events = source.events(monday, sunday)
        except Exception as e:  # network, or the API turned off: say so instead of failing the chat
            return {"text": f"Sorry, I couldn't read your Google Calendar ({str(e)[:160]}).", "kind": "text", "payload": None}

        color = styled(CALENDAR, self.db).color
        waiting = 0
        for r in self.db.items(("proposed", "confirmed")):
            day = self._dated(r)
            if r["calendar_event_id"] or day is None or not monday <= day <= sunday:
                continue  # already in Google Calendar, or not this week
            item = item_model(r)
            timed = not (item.all_day or item.type in ALL_DAY_TYPES)
            events.append({"title": r["title"], "first": day.isoformat(), "last": day.isoformat(),
                           "start": item.start_at.astimezone(self.tz).strftime("%H:%M") if timed else "", "end": "",
                           "calendar": "Added in chat" if from_chat(r["gmail_id"]) else "From your email",
                           "color": color, "location": r["location"] or "",
                           "link": "", "waiting": r["status"] == "proposed"})
            waiting += r["status"] == "proposed"
        events.sort(key=lambda ev: (ev["first"], ev["start"]))

        span = f"{monday.day} {monday:%b}" if monday.month != sunday.month else str(monday.day)
        label = f"{'Next week' if 'next week' in q else 'This week'}, {span} – {sunday.day} {sunday:%b}"
        n = len(events)
        text = f"{label}: {n} event{'s' if n != 1 else ''}." if n else f"{label}: nothing on your calendars. 🌤️"
        if waiting:
            text += f" {waiting} from your email still need{'s' if waiting == 1 else ''} your OK (dashed)."
        return {"text": text, "kind": "week", "payload": {"first": monday.isoformat(), "label": label, "events": events}}

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
        if re.search(r"schedule|calendar|my week|week ahead|next week|busy|plans", q):
            return [self.week(q)]
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
        return [{"text": "I look after your calendar. Try: \"What needs my OK?\", \"Show my schedule this week\", "
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
        """Store your question and the agent's answer; returns the new message ids.

        The model answers when one is set (self.chat, see doraemon.chat); if it can't be
        reached, the fixed keyword answers below take over.
        """
        history = self.db.messages(agent, limit=8)
        ids = [self.db.add_message(agent, "user", question)]
        answers = None
        if self.chat is not None:
            try:
                answers = self.chat.answer(agent, question, history)
            except Exception as e:  # model not running, timed out, or gave nothing back
                log.warning("chat model failed, using fixed answers: %s", e)
        if answers is None:
            answers = self.money_reply(question) if agent == "money" else self.calendar_reply(question)
            if self.chat is not None:
                answers[0]["text"] += "\n(Quick answer: the local model isn't responding right now.)"
        for msg in answers:
            ids.append(self.db.add_message(agent, "agent", msg["text"], msg["kind"], msg["payload"]))
        return ids
