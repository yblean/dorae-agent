"""The two agents you chat with. Each only sees its own area.

Dorae-1 (money) answers about spending; Dorae-2 (calendar) about bills,
appointments, deadlines and trips, and holds the items waiting for your OK.
Each posts a short overview once a day; you ask for the details.

A reply is some text plus, optionally, a card (breakdown bars, a payments
table, item cards, your week from Google Calendar). The local model answers
questions by calling read-only tools (doraemon.chat); the keyword answers here
are the fallback when it isn't running.
"""
import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from doraemon import budgets as bud
from doraemon import coach, foodlog, income
from doraemon.income import CATEGORIES
from doraemon.assistant import last_full_month, month_name, spending as spending_text
from doraemon.calendar_sync import ALL_DAY_TYPES, Schedule
from doraemon.config import Settings
from doraemon.db import Database, from_chat, item_model, transaction_model
from doraemon.display import describe_when
from doraemon.ledger import counted_between, refunded_purchases, spending_totals
from doraemon.networth import KINDS, NetWorth, shift_month
from doraemon.events import draft_event, split_event
from doraemon.fitness import Fitness, suggest_targets
from doraemon.reminders import draft_reminder, short_time, split_request
from doraemon.router import Route, route
from doraemon.rules import name_key
from doraemon.schema import Category

log = logging.getLogger(__name__)

OTHER_NUDGE = 5  # this many payments in Other this month, and Dorae-1's overview points to the Spending page
LABEL_KEY = "fitness:last_label"  # the nutrition label in your last photo, so "2 servings" can redo the sums
LABEL_MINUTES = 30


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
    ("What's my latest purchase?", "Show this month's breakdown", "How am I doing on my budget?",
     "Is my spending going up or down?", "How was {last_month}?"),
    (("Check inbox for receipts", "With Dorae-2's check", "On"),
     ("Budget alerts", "At 80% and 100% of a budget", "Budgets"),
     ("Monthly summary", "1st of each month, also on Telegram", "On")),
)
CALENDAR = Agent(
    "calendar", "Dorae-2", "Calendar & reminders", "#EC4899",
    ("What needs my OK?", "Show my schedule this week", "What's due this week?", "My upcoming trips"),
    (("Check inbox", "Every 15 minutes", "Run now"),
     ("Morning briefing", "Every day", "Briefing"),
     ("Telegram reminders", "Bills, deadlines, deliveries, RSVPs", "Telegram")),
)
FITNESS = Agent(
    "fitness", "Dorae-3", "Fitness & food", "#22C55E",
    ("How am I doing today?", "Protein left today", "Calories this week", "Chicken rice and kopi siew dai",
     "My targets"),
    (("6pm check", "Protein, calories and steps, also on Telegram", "Fitness"),
     ("Steps from Apple Health", "From your OneDrive folder", "Steps"),
     ("Learns your foods", "From what you log and correct", "On")),
)
AGENTS = {a.id: a for a in (MONEY, CALENDAR, FITNESS)}
HELP_TEXT = {  # "what can you do?": answered instantly, in each agent's own lane
    "money": "I look after your spending. Ask me things like: how much did I spend this week, what did I spend on "
             "today, my spending breakdown last month, top merchants, my latest or biggest purchase, did I have a "
             "4.87 payment, my budget, my spending trend, my net worth, my holdings, or how much I kept in "
             "September. You can also set a budget: “set my dining budget to 300”.",
    "calendar": "I look after your calendar and reminders. Ask me things like: what's on tomorrow, my schedule "
                "next week, anything this weekend, do I have anything in November, what's due, my trips, my "
                "reminders, or what needs my OK. You can also say “remind me to call mom at 5pm tmr” or “add "
                "dinner with Sam on friday 7-9pm”.",
    "fitness": "I look after your food, steps and daily targets. Tell me what you ate (“chicken rice and kopi "
               "siew dai”, “nutrisoy no sugar 250ml”), or send a photo of a nutrition label or your meal (📷), and I'll "
               "work out the calories and macros for you to Log. Ask "
               "me how you're doing today, protein left, calories this week, steps, or your targets; or say “change my "
               "daily calories to 1950” or “lower my calories by 100”. At 6pm I check what's left and how to close the gap.",
}
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


def label_in(label: str) -> str:
    """'September' -> 'in September', 'October so far' -> 'in October so far', 'today (Sun 11 Oct)' as it is."""
    if label == "all time":
        return "overall"
    return f"in {label}" if label.split()[0].lower() in _MONTHS else label


def short_clock(hhmm: str) -> str:
    """'09:30' -> '9:30am', '14:00' -> '2pm'."""
    t = time.fromisoformat(hhmm)
    return f"{t.hour % 12 or 12}{f':{t.minute:02d}' if t.minute else ''}{'am' if t.hour < 12 else 'pm'}"


class Brain:
    def __init__(self, db: Database, settings: Settings, fx=None,
                 schedule: Callable[[], Schedule | None] | None = None) -> None:
        self.db, self.settings, self.fx = db, settings, fx
        self.schedule = schedule  # your Google calendars, or None until reading them is approved
        self.chat = None  # doraemon.chat.AgentChat when a chat model is set
        self.overview_lock = threading.Lock()  # the briefing timer and opening the chat can race
        self.last_route = ""  # how the latest question was answered: a router intent, "model", ...
        self.tz = ZoneInfo(settings.timezone)
        self.home = settings.home_currency
        self.nw = NetWorth(db, self.home, fx)
        self.fit = Fitness(db, settings.timezone)  # Dorae-3
        self.food_backend = None  # the model that reads what you ate (doraemon.foodlog); made when first needed

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
        return self.ledger_between(*self.month_range(month))

    def ledger_between(self, first: date | None, last: date | None):
        """Counted payments from the first to the last day (both included, your timezone), duplicates dropped.
        A refund counts on the day of the purchase it reverses (doraemon.ledger.counted_between)."""
        rows = [r for r in self.db.transactions() if r["status"] == "counted"]
        return counted_between([transaction_model(r) for r in rows], first, last, self.tz)

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
        card["text"] += extra
        overall = next((s for s in self.budget_statuses() if s.category == bud.TOTAL), None)
        if overall:
            card["text"] += " " + bud.describe(overall, self.home)
        unsorted = sum(1 for t in self.ledger(month) if t.category == Category.OTHER and not t.is_refund)
        if unsorted >= OTHER_NUDGE:
            card["text"] += f" {unsorted} payments this month are in Other: sort them out on the Spending page."
        card["text"] += " Ask me for any month, category, merchant or your budgets."
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

    def merchants(self, kept, n: int = 5) -> list[tuple[str, Decimal, int]]:
        """The merchants most money went to, as (name, total in the home currency, payments)."""
        sums = spending_totals(kept, self.home, self.fx)
        by_merchant: dict[str, list] = {}
        for t in kept:
            if id(t) not in sums.home_amounts:
                continue
            key = name_key(t.merchant) or t.merchant.lower()
            entry = by_merchant.setdefault(key, [t.merchant, Decimal(0), 0])
            entry[1] += -sums.home_amounts[id(t)] if t.is_refund else sums.home_amounts[id(t)]
            entry[2] += 1
        return [tuple(e) for e in sorted(by_merchant.values(), key=lambda e: -e[1])[:n]]

    def top_merchants_of(self, kept, label: str, month: str | None = None) -> dict:
        top = self.merchants(kept)
        if not top:
            return {"text": f"No payments in {label}.", "kind": "text", "payload": None}
        lines = [f"{i}. {name}: {money(total, self.home)}" + (f" ({n} payments)" if n > 1 else "")
                 for i, (name, total, n) in enumerate(top, 1)]
        card = self.summary(kept, label, month)
        card["text"] = f"Where your money went in {label}:\n" + "\n".join(lines)
        return card

    # --- monthly trend ------------------------------------------------------------

    def month_shift(self, month: str, n: int) -> str:
        y, m = divmod(int(month[:4]) * 12 + int(month[5:]) - 1 + n, 12)
        return f"{y}-{m + 1:02d}"

    def first_month(self) -> str | None:
        """The first month your payments are complete for: the month your read email starts in, or the next one
        if it starts after the 7th. A payment from before then (say, on a later statement) is all that month has."""
        row = self.db.conn.execute("SELECT MIN(sent_at) FROM processed_emails WHERE sent_at IS NOT NULL").fetchone()
        if row[0]:
            start = datetime.fromisoformat(row[0]).astimezone(self.tz).date()
            month = start.strftime("%Y-%m")
            return month if start.day <= 7 else self.month_shift(month, 1)
        months = [r["purchased_at"][:7] for r in self.db.transactions() if r["status"] == "counted"]
        return min(months) if months else None

    def month_total(self, month: str, category: str | None = None) -> Decimal:
        sums = spending_totals(self.ledger(month), self.home, self.fx)
        return sums.by_category.get(category, Decimal(0)) if category else sums.total

    def trend(self, months: int = 6, category: str | None = None) -> dict:
        """Spending per month for the last few months (this one so far), as a column chart card."""
        this = self.this_month()
        start = self.month_shift(this, -(max(2, min(months, 12)) - 1))
        start = max(start, min(self.first_month() or this, this))
        series = []
        month = start
        while month <= this:
            series.append({"month": month, "label": month_name(month)[:3], "name": month_name(month),
                           "total": str(self.month_total(month, category)), "partial": month == this})
            month = self.month_shift(month, 1)
        limit = self.db.budgets().get(category or bud.TOTAL)
        what = f"{category.capitalize()} spending" if category else "Spending"
        payload = {"label": f"{what} by month", "currency": self.home, "category": category,
                   "budget": str(limit) if limit else None, "months": series}
        return {"text": self.trend_text(payload), "kind": "trend", "payload": payload}

    def trend_text(self, p: dict) -> str:
        """Is spending going up or down: the last full month against the one before, and this month's pace."""
        months, home = p["months"], self.home
        full = [m for m in months if not m["partial"]]
        parts = []
        if len(full) >= 2:
            last, before = Decimal(full[-1]["total"]), Decimal(full[-2]["total"])
            if before > 0:
                change = int((last - before) / before * 100)
                way = "up" if change > 0 else "down" if change < 0 else "level"
                parts.append(f"{full[-1]['name']} came to {money(last, home)}, {way}"
                             + (f" {abs(change)}%" if change else "") + f" on {full[-2]['name']}.")
            else:
                parts.append(f"{full[-1]['name']} came to {money(last, home)}.")
        elif full:
            parts.append(f"{full[-1]['name']} came to {money(Decimal(full[-1]['total']), home)}.")
        now = months[-1]
        spent = Decimal(now["total"])
        pace = bud.cents(spent / self.today().day * bud.days_in(now["month"]))
        parts.append(f"{now['name']} so far: {money(spent, home)}, on pace for about {money(pace, home)}.")
        if direction := self.direction(p):
            parts.append(direction)
        if len(full) < 2:
            parts.append("A trend needs a couple more months of payments.")
        if p["budget"]:
            parts.append(f"Your monthly budget is {money(Decimal(p['budget']), home)}.")
        return " ".join(parts)

    def direction(self, p: dict) -> str:
        """This month's pace against last month, worked out here so the model never has to compare numbers."""
        full = [m for m in p["months"] if not m["partial"]]
        if not full or Decimal(full[-1]["total"]) <= 0:
            return ""
        now, last = p["months"][-1], Decimal(full[-1]["total"])
        pace = Decimal(now["total"]) / self.today().day * bud.days_in(now["month"])
        change = int((pace - last) / last * 100)
        early = " (early in the month, so this can still change a lot)" if self.today().day < 10 else ""
        if abs(change) < 5:
            return f"So {now['name']} is heading for about the same as {full[-1]['name']}{early}."
        way = "UP" if change > 0 else "DOWN"
        return f"So spending is going {way}: {now['name']} is on pace for {abs(change)}% {'more' if change > 0 else 'less'} than {full[-1]['name']}{early}."

    # --- budgets -------------------------------------------------------------

    def budget_statuses(self, month: str | None = None) -> list[bud.Status]:
        month = month or self.this_month()
        sums = spending_totals(self.ledger(month), self.home, self.fx)
        return bud.statuses(self.db.budgets(), sums.by_category, sums.total, month, self.today())

    def averages(self, months: int = 3) -> dict[str, Decimal]:
        """Average spending per category (and TOTAL) over the last few full months that have payments."""
        first = self.first_month()
        sums = []
        month = last_full_month(self.settings)
        while len(sums) < months and first and month >= first:
            sums.append(spending_totals(self.ledger(month), self.home, self.fx))
            month = self.month_shift(month, -1)
        if not sums:
            return {}
        out: dict[str, Decimal] = {}
        for t in sums:
            for c, a in [*t.by_category.items(), (bud.TOTAL, t.total)]:
                out[c] = out.get(c, Decimal(0)) + a
        return {c: bud.cents(a / len(sums)) for c, a in out.items()}

    def budget_card(self, month: str | None = None, draft: tuple[str, Decimal] | None = None) -> dict:
        """Every budget's progress this month as a card; `draft`: a budget you asked for, waiting for Save."""
        month = month or self.this_month()
        stats = self.budget_statuses(month)
        budgets = self.db.budgets()
        spent = spending_totals(self.ledger(month), self.home, self.fx).by_category
        unbudgeted = sorted(((c, a) for c, a in spent.items() if c not in budgets and a > 0),
                            key=lambda kv: -kv[1])[:3]
        rows = [{"category": s.category, "label": s.label, "limit": str(s.limit), "spent": str(s.spent),
                 "left": str(s.left), "used": s.used, "pace": s.pace, "state": s.state, "days_left": s.days_left,
                 "per_day": str(s.per_day) if s.per_day is not None else None} for s in stats]
        payload = {"month": month, "label": month_name(month), "currency": self.home, "rows": rows,
                   "unbudgeted": [[c, str(a)] for c, a in unbudgeted],
                   "draft": {"category": draft[0], "amount": str(draft[1])} if draft else None}
        if draft:
            name = "overall" if draft[0] == bud.TOTAL else draft[0]
            current = budgets.get(draft[0])
            text = (f"Here's a monthly {name} budget of {money(draft[1], self.home)}"
                    + (f" (it's {money(current, self.home)} now)" if current else "")
                    + ". Press Save on the card to set it.")
            avg = self.averages().get(draft[0])
            if avg:
                text += f" Lately you've spent {money(avg, self.home)} a month on average."
        elif not stats:
            text = ("You haven't set any budgets yet. Set a monthly budget for all your spending or a category "
                    "on the Budgets page, or tell me, like “set my dining budget to 300”.")
        else:
            text = self.budget_advice(stats, month)
        return {"text": text, "kind": "budgets", "payload": payload}

    def budget_facts(self, month: str | None = None) -> dict:
        """What the chat model gets to give advice from: every number is worked out here, not by the model."""
        month = month or self.this_month()
        stats = self.budget_statuses(month)
        kept = self.ledger(month)
        home, avg, budgets = self.home, self.averages(), self.db.budgets()
        fmt = lambda a: f"{home} {a:,.2f}"
        spent = spending_totals(kept, home, self.fx).by_category
        day = self.today().day if month == self.this_month() else bud.days_in(month)
        facts: dict = {"month": month_name(month), "day": f"day {day} of {bud.days_in(month)}",
                       "tips": self.budget_tips(stats, kept, spent, avg if month == self.this_month() else {},
                                                          budgets)}
        if not stats:
            facts["no_budgets_set"] = ("Tell the user they can set budgets on the Budgets page or by asking, e.g. "
                                       "'set my dining budget to 300'. Suggest amounts near their monthly averages.")
        facts["budgets"] = []
        for s in stats:
            entry = {"budget": s.label, "status": s.state.replace("_", " "), "summary": bud.describe(s, home),
                     "limit": fmt(s.limit), "spent": fmt(s.spent), "used": f"{s.used}%", "month_gone": f"{s.pace}%"}
            if s.per_day is not None:
                entry["can_spend_per_day_to_stay_within"] = fmt(s.per_day)
            if (usual := self.vs_usual(s.label, s.projected if s.days_left > 0 else s.spent, avg.get(s.category))):
                entry["compared_with_usual"] = usual
            facts["budgets"].append(entry)
        facts["spending_without_a_budget"] = {c: fmt(a) for c, a in sorted(spent.items(), key=lambda kv: -kv[1])
                                              if c not in budgets and a > 0}
        if avg:
            facts["usual_monthly_spending"] = {c: fmt(a) for c, a in avg.items()}
        return facts

    def vs_usual(self, label: str, heading_for: Decimal, usual: Decimal | None) -> str:
        """'Dining is heading for SGD 58.00, below your usual SGD 69.00 a month.' Empty without a usual amount."""
        if not usual:
            return ""
        way = ("above" if heading_for > usual * Decimal("1.1") else
               "below" if heading_for < usual * Decimal("0.9") else "about the same as")
        return (f"{label} is heading for {money(heading_for, self.home)}, {way} your usual "
                f"{money(usual, self.home)} a month.")

    def budget_tips(self, stats: list[bud.Status], kept, spent: dict[str, Decimal], avg: dict[str, Decimal],
                    budgets: dict[str, Decimal]) -> list[str]:
        """Concrete things to do, most urgent first, worked out from the numbers (the model only words them)."""
        home, tips = self.home, []
        order = {"over": 0, "at_risk": 1, "on_track": 2}
        for s in sorted(stats, key=lambda s: (order[s.state], -s.used)):
            mine = kept if s.category == bud.TOTAL else [t for t in kept if t.category.value == s.category]
            top = self.merchants(mine, 2)
            where = (" Most of it went to " + " and ".join(f"{n} ({money(a, home)})" for n, a, _ in top) + "."
                     if top else "")
            what = "spending" if s.category == bud.TOTAL else s.category
            if s.state == "over":
                tips.append(f"{s.label} is already {money(-s.left, home)} over its {money(s.limit, home)} budget."
                            f"{where} Hold off on more {what} until next month.")
            elif s.state == "at_risk":
                tips.append(f"{s.label} is ahead of pace: keep {what} to {money(s.per_day, home)} a day for the "
                            f"{s.days_left} days left to stay within {money(s.limit, home)}.{where}")
        for c, a in sorted(spent.items(), key=lambda kv: -kv[1]):
            usual = avg.get(c)
            if c not in budgets and a > 0 and usual and self.today().day >= 5:
                heading = bud.cents(a / self.today().day * bud.days_in(self.this_month()))
                if heading > usual * Decimal("1.2"):
                    tips.append(self.vs_usual(c.capitalize(), heading, usual) + " It has no budget yet: setting one "
                                "would help.")
        if not tips and stats:
            overall = next((s for s in stats if s.category == bud.TOTAL), stats[0])
            tips.append("Everything is on track." + (f" Keep {'spending' if overall.category == bud.TOTAL else overall.category}"
                        f" to about {money(overall.per_day, home)} a day." if overall.per_day else ""))
        return tips[:4]

    def budget_advice(self, stats: list[bud.Status], month: str) -> str:
        """The fixed-answer version of the advice: the budget most at risk, and where its money went."""
        order = {"over": 0, "at_risk": 1, "on_track": 2}
        worst = sorted(stats, key=lambda s: (order[s.state], -s.used))[0]
        text = bud.describe(worst, self.home)
        if worst.state == "on_track":
            return text + (" All your budgets are on track. Keep it up! 🎉" if len(stats) > 1 else " You're on track. 🎉")
        kept = self.ledger(month)
        mine = kept if worst.category == bud.TOTAL else [t for t in kept if t.category.value == worst.category]
        top = self.merchants(mine, 2)
        if top:
            text += " Most went to " + " and ".join(f"{n} ({money(a, self.home)})" for n, a, _ in top) + "."
        behind = [s for s in stats if s is not worst and s.state != "on_track"]
        if behind:
            text += " Also keep an eye on " + ", ".join(s.label.lower() for s in behind) + "."
        return text

    def budget_alerts(self, quiet: bool = False) -> list[int]:
        """Post a message when a budget passes 80% or 100% this month, once per level. Returns message ids.
        `quiet`: start over from where each budget is now, e.g. right after you changed one and saw its card,
        so raising a budget lets its alerts come again."""
        month = self.this_month()
        key = f"budget_alerts:{month}"
        stats = self.budget_statuses(month)
        if quiet:
            seen = {s.category: lv for s, lv in bud.new_alerts(stats, {})}
            self.db.set_setting(key, ",".join(f"{k}={v}" for k, v in seen.items()) or None)
            return []
        seen = {k: int(v) for k, v in (p.split("=") for p in (self.db.get_setting(key) or "").split(",") if p)}
        ids = []
        found = bud.new_alerts(stats, seen)
        for s, level in found:
            seen[s.category] = level
            name = "your overall budget" if s.category == bud.TOTAL else f"your {s.category} budget"
            if level >= 100:
                text = f"⚠️ You've gone over {name}. " + bud.describe(s, self.home)
            else:
                text = f"Heads up: you've used {s.used}% of {name}. " + bud.describe(s, self.home)
            card = self.budget_card(month)
            ids.append(self.db.add_message("money", "agent", text, card["kind"], card["payload"]))
        if found:
            self.db.set_setting(key, ",".join(f"{k}={v}" for k, v in seen.items()))
        return ids

    def money_reply(self, q: str) -> list[dict]:
        asked = bud.parse_request(q)
        if asked:
            return [self.budget_card(draft=asked)]
        q = q.lower()
        if re.search(r"net ?worth|how much am i worth|my assets", q):
            return [self.networth_card()]
        if re.search(r"income|earn|salary|how much did i make|did i save|savings rate", q):
            return [self.income_card("last_month" if "last month" in q else None)]
        if re.search(r"investment|holding|portfolio|stocks|positions", q):
            return [self.holdings_card()]
        if re.search(r"budget|overspen|spending limit|\bcap\b|stay within|save money|cut back|spend less", q):
            return [self.budget_card()]
        if re.search(r"trend|going up|going down|over time|by month|per month|monthly|month to month|"
                     r"increas|decreas|compared? to last|chart", q):
            category = next((c for word, c in _CATEGORY_WORDS.items() if re.search(rf"\b{word}", q)), None)
            return [self.trend(category=category if category in bud.CATEGORIES else None)]
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
                and (not types or r["type"] in types) and not r["done_at"]]
        return [r["id"] for r in sorted(rows, key=lambda r: r["start_at"])]

    def calendar_events(self, first: date, last: date) -> tuple[list[dict], str]:
        """Events from all your Google calendars plus email and chat items not in Google yet, soonest first.
        Returns (events, problem): without Google access, only the items, and the problem says why."""
        events, problem = [], ""
        source = self.schedule() if self.schedule else None
        if source is not None:
            try:
                events = source.events(first, last)
            except Exception as e:  # network, or the API turned off: say so instead of failing
                problem = str(e)[:160]
        color = styled(CALENDAR, self.db).color
        # Items Doraemon put in Google Calendar show from there. One added since the calendars were last read
        # (they're remembered for a few minutes), or with Google unreadable, shows from here instead.
        in_google = {e["id"] for e in events if e.get("id")}
        for r in self.db.items(("proposed", "confirmed")):
            day = self._dated(r)
            if (r["calendar_event_id"] and r["calendar_event_id"] in in_google) or r["done_at"] or day is None \
                    or not first <= day <= last:
                continue  # already in Google Calendar, done, or not in these days
            item = item_model(r)
            timed = not (item.all_day or item.type in ALL_DAY_TYPES)
            events.append({"title": r["title"], "first": day.isoformat(), "last": day.isoformat(),
                           "start": item.start_at.astimezone(self.tz).strftime("%H:%M") if timed else "", "end": "",
                           "calendar": "Added in chat" if from_chat(r["gmail_id"]) else "From your email",
                           "color": color, "location": r["location"] or "",
                           "link": "", "waiting": r["status"] == "proposed"})
        events.sort(key=lambda ev: (ev["first"], ev["start"]))
        return events, problem

    def week(self, q: str) -> dict:
        """Your week (Monday to Sunday) from all your Google calendars, plus email items not in it yet."""
        monday = self.today() - timedelta(days=self.today().weekday())
        if "next week" in q:
            monday += timedelta(days=7)
        sunday = monday + timedelta(days=6)
        if self.schedule is None or self.schedule() is None:
            return {"text": "To show your week I need to read your Google calendars (read-only). Run "
                            "python -m doraemon.calendar_sync connect once and approve.", "kind": "text", "payload": None}
        events, problem = self.calendar_events(monday, sunday)
        if problem:
            return {"text": f"Sorry, I couldn't read your Google Calendar ({problem}).", "kind": "text", "payload": None}
        waiting = sum(1 for e in events if e.get("waiting"))

        span = f"{monday.day} {monday:%b}" if monday.month != sunday.month else str(monday.day)
        label = f"{'Next week' if 'next week' in q else 'This week'}, {span} – {sunday.day} {sunday:%b}"
        n = len(events)
        text = f"{label}: {n} event{'s' if n != 1 else ''}." if n else f"{label}: nothing on your calendars. 🌤️"
        if waiting:
            text += f" {waiting} from your email still need{'s' if waiting == 1 else ''} your OK (dashed)."
        return {"text": text, "kind": "week", "payload": {"first": monday.isoformat(), "label": label, "events": events}}

    def monthly_summary(self, month: str | None = None) -> dict:
        """A month in review (last month by default): spending against the month before and your budget, where it
        went and what changed most, the biggest purchase, what you kept from your income and your net worth.
        Every number is worked out here; it's posted on the 1st (Runtime.monthly_tick) and answers "monthly summary"."""
        month = month or last_full_month(self.settings)
        prev, home = self.month_shift(month, -1), self.home
        kept = self.ledger(month)
        card = self.summary(kept, month_name(month), month)
        p = card["payload"]
        if not p["count"]:
            return {"text": f"No payments in {month_name(month)} to sum up yet.", "kind": "text", "payload": None}
        total = Decimal(p["total"])
        text = f"📅 {month_name(month)} in review: you spent {money(total, home)} across {p['count']} payments"
        compare = (self.first_month() or month) <= prev
        before = self.month_total(prev) if compare else Decimal(0)
        if before:
            diff = total - before
            text += (f", {money(abs(diff), home)} ({int(abs(diff) / before * 100)}%) {'more' if diff > 0 else 'less'} "
                     f"than {month_name(prev)}." if diff else f", the same as {month_name(prev)}.")
        else:
            text += "."
        overall = next((s for s in self.budget_statuses(month) if s.category == bud.TOTAL), None)
        if overall:
            text += (f" That's {overall.used}% of your {money(overall.limit, home)} budget"
                     + (f", {money(overall.spent - overall.limit, home)} over." if overall.spent > overall.limit
                        else ", within it."))
        if p["rows"]:
            text += f" Most went to {p['rows'][0][0]} ({money(Decimal(p['rows'][0][1]), home)})."
        if before:
            now_by = dict((c, Decimal(a)) for c, a in p["rows"])
            then_by = spending_totals(self.ledger(prev), home, self.fx).by_category
            changes = [(c, now_by.get(c, Decimal(0)) - then_by.get(c, Decimal(0))) for c in {*now_by, *then_by}]
            cat, change = max(changes, key=lambda kv: abs(kv[1]), default=("", Decimal(0)))
            if abs(change) >= 20:
                text += f" The biggest change: {cat} {'up' if change > 0 else 'down'} {money(abs(change), home)}."
        sums = spending_totals(kept, home, self.fx)
        refunded = {id(p) for p in refunded_purchases(kept).values()}  # a cancelled booking wasn't really spent
        spends = [t for t in kept if not t.is_refund and id(t) in sums.home_amounts and id(t) not in refunded]
        if spends:
            big = max(spends, key=lambda t: sums.home_amounts[id(t)])
            day = big.purchased_at.astimezone(self.tz)
            text += f" Biggest purchase: {big.merchant}, {money(sums.home_amounts[id(big)], home)} on {day.day} {day:%b}."
        got = income.total(income.in_month(self.db, month, home, self.fx, self.today()))
        if got > 0:
            saved = got - total
            text += (f" From {money(got, home)} of income you kept {money(saved, home)} ({int(saved / got * 100)}%)."
                     if saved >= 0 else f" You spent {money(-saved, home)} more than your {money(got, home)} of income.")
        end, end_before = self.month_range(month)[1], self.month_range(prev)[1]
        worth, worth_before = self.nw.worth_at(end), self.nw.worth_at(end_before)
        if worth.counted:
            text += f" Net worth at the end of the month: {money(worth.total, home)}"
            if worth_before.counted and worth_before.total:
                change = worth.total - worth_before.total
                text += f", {'up' if change >= 0 else 'down'} {money(abs(change), home)}."
            else:
                text += "."
        card["text"] = text
        card["payload"]["label"] = f"{month_name(month)} in review"
        return card

    FIND_DAYS = 120  # how far ahead "when is my ..." looks
    FIND_FILLER = {"my", "the", "a", "an", "next", "upcoming", "coming", "up", "is", "are", "when", "appointment",
                   "appointments", "event", "events", "meeting", "scheduled", "booked", "on", "at", "for", "with",
                   "to", "of", "do", "i", "have", "any", "due", "planned", "time", "day", "date", "our"}

    def find_card(self, what: str) -> dict:
        """'When is my dentist appointment?': the soonest event or item whose title has the words you used, from all
        your calendars and your email, in the next FIND_DAYS days."""
        words = [w for w in re.findall(r"[a-z0-9]+", what.lower()) if w not in self.FIND_FILLER and len(w) > 1]
        today, now = self.today(), datetime.now(self.tz)
        events, problem = self.calendar_events(today, today + timedelta(days=self.FIND_DAYS))
        upcoming = [e for e in events if not (e["first"] == today.isoformat() and e["start"]
                                              and (e["end"] or e["start"]) < now.strftime("%H:%M"))]
        if words:
            scored = [(sum(1 for w in words if w in e["title"].lower()), e) for e in upcoming]
            best = max((s for s, _ in scored), default=0)
            found = [e for s, e in scored if best and s == best]
        else:  # "when is my next appointment": the next thing with a time
            found = [e for e in upcoming if e["start"]][:1]
        if not found:
            return {"text": f"I couldn't find “{what}” in your calendars or email in the next {self.FIND_DAYS // 30} months."
                            + (f" (I couldn't read Google Calendar: {problem}.)" if problem else ""),
                    "kind": "text", "payload": None}
        first = found[0]
        d = date.fromisoformat(first["first"])
        gap = (d - today).days
        when = f"{d:%a} {d.day} {d:%b}" + (f", {short_clock(first['start'])}" if first["start"] else " (all day)")
        text = (f"{first['title']}: {when}, {'today' if gap == 0 else 'tomorrow' if gap == 1 else f'in {gap} days'}"
                + (f" · {first['location']}" if first["location"] else "") + "."
                + (f" {len(found) - 1} more after that." if len(found) > 1 else ""))
        days: dict[str, dict] = {}
        for e in found[:5]:
            day = date.fromisoformat(e["first"])
            days.setdefault(e["first"], {"date": e["first"], "label": f"{day:%a} {day.day} {day:%b}", "events": []})
            days[e["first"]]["events"].append(e)
        return {"text": text, "kind": "days", "payload": {"label": f"“{what}”", "days": list(days.values())}}

    def days_range(self, when: str) -> tuple[date, date, str]:
        """(first day, last day, label) for a calendar question's time (doraemon.router.spoken_when)."""
        today = self.today()
        name = lambda d: f"{d:%a} {d.day} {d:%b}"
        monday = today - timedelta(days=today.weekday())
        kind, _, rest = when.partition(":")
        if kind == "tomorrow":
            d = today + timedelta(days=1)
            return d, d, f"Tomorrow, {name(d)}"
        if kind in ("this_weekend", "next_weekend"):
            saturday = monday + timedelta(days=12 if kind == "next_weekend" else 5)
            first, last = max(saturday, today), saturday + timedelta(days=1)  # on a Sunday, what's left of it
            span = name(first) + (f" – {name(last)}" if first != last else "")
            return first, last, f"{'This' if kind == 'this_weekend' else 'Next'} weekend, {span}"
        if kind == "last_week":
            first, last = monday - timedelta(days=7), monday - timedelta(days=1)
            return first, last, f"Last week, {name(first)} – {name(last)}"
        if kind in ("this_month", "next_month", "month"):
            if kind == "month":
                n = int(rest)
                month = f"{today.year if n >= today.month else today.year + 1}-{n:02d}"  # the coming one
            else:
                month = self.this_month() if kind == "this_month" else self.month_shift(self.this_month(), 1)
            first, last = self.month_range(month)
            if month == self.this_month():  # the rest of this month
                return today, last, f"{month_name(month)} from today"
            return first, last, month_name(month)
        if kind == "weekday":
            day, _, nxt = rest.partition(":")
            d = (monday + timedelta(days=7 + int(day))) if nxt else today + timedelta(days=(int(day) - today.weekday()) % 7)
            return d, d, name(d) + (" (today)" if d == today else "")
        if kind == "date":
            from doraemon.dates import resolve_spoken
            found, _ = resolve_spoken(rest, datetime.now(self.tz), self.settings.timezone, self.settings.date_order)
            if found is not None:
                return found.date(), found.date(), name(found.date())
        return today, today, f"Today, {name(today)}"

    def days_card(self, first: date, last: date, label: str) -> dict:
        """What's on from the first to the last day: all your Google calendars plus items not in them yet.
        A short span lists every day ("Free" ones too); a month lists only the days with something on."""
        events, problem = self.calendar_events(first, last)
        if problem:
            return {"text": f"Sorry, I couldn't read your Google Calendar ({problem}).", "kind": "text", "payload": None}
        days, d = [], first
        while d <= last:
            iso = d.isoformat()
            on = [e for e in events if e["first"] <= iso <= e["last"]]
            if on or (last - first).days < 7:
                days.append({"date": iso, "label": f"{d:%a} {d.day} {d:%b}", "events": on})
            d += timedelta(days=1)
        n = len(events)
        text = f"{label}: {n} event{'s' if n != 1 else ''}." if n else f"{label}: nothing on your calendars. 🌤️"
        waiting = sum(1 for e in events if e.get("waiting"))
        if waiting:
            text += f" {waiting} from your email still need{'s' if waiting == 1 else ''} your OK."
        if self.schedule is None or self.schedule() is None:
            text += " (Only what's in Doraemon: connect Google Calendar to see your other calendars.)"
        return {"text": text, "kind": "days", "payload": {"label": label, "days": days}}

    BILL_DAYS, TRIP_DAYS = 7, 14  # how far ahead the briefing looks for bills and trips

    def briefing(self) -> list[dict]:
        """Dorae-2's daily briefing: today and tomorrow from all your calendars, bills due this week, trips coming
        up, Telegram reminders going out today and what needs your OK. Fixed rules pick everything; no model."""
        now = datetime.now(self.tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        events, problem = self.calendar_events(today, tomorrow)
        on = lambda d: [e for e in events if e["first"] <= d.isoformat() <= e["last"]]
        days = [{"label": "Today", "date": today.isoformat(), "events": on(today)},
                {"label": "Tomorrow", "date": tomorrow.isoformat(), "events": on(tomorrow)}]
        bills = self.agenda(self.BILL_DAYS, ("bill",))
        trips = [i for i in self.agenda(self.TRIP_DAYS, ("flight", "hotel", "other_travel"))
                 if self._dated(self.db.get("item", i)) > tomorrow]  # today's and tomorrow's are listed above
        reminders = []
        for r in self.db.upcoming_reminders():
            at = datetime.fromisoformat(r["remind_at"]).astimezone(self.tz)
            if at.date() == today and at >= now - timedelta(minutes=1):
                reminders.append({"time": at.strftime("%H:%M"), "title": self.db.get("item", r["item_id"])["title"]})
        upcoming, past = self.pending()

        hello = "Good morning!" if now.hour < 12 else "Good afternoon!" if now.hour < 18 else "Good evening!"
        parts = [f"{hello} Here's {today:%A} {today.day} {today:%B}."]
        n_today, n_tomorrow = len(days[0]["events"]), len(days[1]["events"])
        if n_today:
            first = next((e for e in days[0]["events"] if e["start"]), days[0]["events"][0])
            starts = f", starting with {first['title']} at {short_clock(first['start'])}" if first["start"] else ""
            parts.append(f"{n_today} thing{'s' if n_today != 1 else ''} today{starts}.")
        else:
            parts.append("Nothing on today. 🌤️")
        if n_tomorrow:
            parts.append(f"{n_tomorrow} tomorrow.")
        if bills:
            rows = [self.db.get("item", i) for i in bills]
            total = self.bill_total(rows)
            parts.append(f"{len(bills)} bill{'s' if len(bills) != 1 else ''} due in the next {self.BILL_DAYS} days"
                         + (f" ({total})." if total else "."))
        if trips:
            r = self.db.get("item", trips[0])
            parts.append(f"Trip coming up: {r['title']} on {self._dated(r):%a} {self._dated(r).day} {self._dated(r):%b}"
                         + (f", and {len(trips) - 1} more." if len(trips) > 1 else "."))
        if reminders:
            parts.append(f"{len(reminders)} Telegram reminder{'s' if len(reminders) != 1 else ''} going out today.")
        if upcoming:
            parts.append(f"{len(upcoming)} thing{'s' if len(upcoming) != 1 else ''} from your email need your OK.")
        if past:
            parts.append(f"({len(past)} already passed: ask \"What already passed?\")")
        if problem:
            parts.append(f"(I couldn't read your Google Calendar: {problem})")
        msgs = [{"text": " ".join(parts), "kind": "briefing",
                 "payload": {"days": days, "bills": bills, "trips": trips, "reminders": reminders}}]
        if upcoming:
            msgs.append({"text": "These need your OK, soonest first:" if len(upcoming) <= 5 else
                         "The soonest that need your OK (ask \"What needs my OK?\" for all of them):",
                         "kind": "items", "payload": {"ids": upcoming[:5], "more": max(0, len(upcoming) - 5)}})
        return msgs

    def bill_total(self, rows) -> str:
        """'SGD 120.50' when every bill has an amount in one currency, else ''."""
        if not rows or any(not r["amount"] for r in rows) or len({r["currency"] for r in rows}) != 1:
            return ""
        return money(sum(Decimal(r["amount"]) for r in rows), rows[0]["currency"])

    def briefing_time(self) -> time | None:
        """When the briefing goes out each day, or None when it's turned off."""
        value = self.settings.briefing_time.strip().lower()
        if value in ("", "off"):
            return None
        try:
            return time.fromisoformat(value)
        except ValueError:
            log.warning("DORAEMON_BRIEFING_TIME=%r isn't a time like 08:00; using 08:00", value)
            return time(8)

    def morning_tick(self, now: datetime | None = None) -> list[dict]:
        """Post the briefing once it's time and it hasn't gone out today. Returns what was posted."""
        at = self.briefing_time()
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        if at is None or now.time() < at:
            return []
        return self.ensure_overview("calendar", scheduled=True)

    def briefing_telegram(self, msg: dict) -> str:
        """The briefing as a plain-text Telegram message (titles come from email, so no formatting)."""
        p = msg["payload"]
        lines = ["☀️ " + msg["text"].split(". ")[0] + "."]
        for day in p["days"]:
            lines += ["", day["label"]]
            for e in day["events"]:
                timed = e["start"] and e["first"] == day["date"]
                lines.append(f"• {short_clock(e['start']) if timed else 'All day'}  {e['title']}"
                             + (" (needs your OK)" if e.get("waiting") else ""))
            if not day["events"]:
                lines.append("• Nothing on")
        for label, ids in (("Bills due soon", p["bills"]), ("Trips coming up", p["trips"])):
            rows = [r for r in (self.db.get("item", i) for i in ids) if r]
            if rows:
                lines += ["", label]
                for r in rows:
                    d = self._dated(r)
                    lines.append(f"• {r['title']} · {d:%a} {d.day} {d:%b}"
                                 + (f" · {r['currency']} {r['amount']}" if r["amount"] else ""))
        if p["reminders"]:
            lines += ["", "Reminders today"] + [f"• {short_clock(r['time'])}  {r['title']}" for r in p["reminders"]]
        waiting = len(self.pending()[0])
        if waiting:
            lines += ["", f"{waiting} thing{'s' if waiting != 1 else ''} from your email need your OK: open Doraemon to check."]
        return "\n".join(lines)

    def event_reply(self, ask, question: str) -> dict:
        """Draft the event you asked for (doraemon.events) as a card waiting for Confirm."""
        result, item_id = draft_event(self.db, self.settings.timezone, self.settings.date_order, ask.title, ask.when,
                                      question, datetime.now(self.tz), end=ask.end)
        if item_id is None:
            return {"text": result["error"], "kind": "text", "payload": None}
        status = self.db.get("item", item_id)["status"]
        if result.get("already_there"):
            return {"text": f"You already have that one: “{result['title']}”, {result['when']}.",
                    "kind": "items" if status == "proposed" else "agenda", "payload": {"ids": [item_id]}}
        return {"text": f"Here's your event: “{result['title']}”, {result['when']}. Press Confirm to add it to your "
                        "Doraemon calendar, or change it first under Edit.",
                "kind": "items", "payload": {"ids": [item_id]}}

    # --- Dorae-3 -----------------------------------------------------------------------------------

    def fitness_first(self, question: str, previous: str | None, history: list | None = None) -> list[dict] | None:
        """What Dorae-3 answers before the usual routes: a target you set, ideas for what to eat (the model plans
        them, from what's left and foods that fit), suggested targets, or food to log."""
        if card := self.label_amount(question):  # "2 servings", just after a label photo
            self.last_route = "label"
            return [card]
        if coach.FOOD_IDEAS.search(question) and not coach.parse_target(question, self.fit.targets()):
            if self.chat is not None:
                try:
                    self.last_route = "model"
                    return self.chat.answer("fitness", question, history or [])
                except Exception as e:  # the model isn't running: the app's own ideas instead
                    log.warning("meal ideas from the model failed: %s", e)
            self.last_route = "ideas"
            return [coach.ideas_card(self.fit)]
        if subject := coach.nutrition_subject(question):  # "how many calories in a bowl of greek yogurt?"
            self.last_route = "nutrition"
            return [self.nutrition_reply(subject)]
        if coach.FOOD_QUESTION.search(question) and self.chat is not None:  # "which has more protein, tofu or eggs?"
            try:
                self.last_route = "model"
                return self.chat.answer("fitness", question, history or [])
            except Exception as e:
                log.warning("food question for the model failed: %s", e)
        if target := coach.parse_target(question, self.fit.targets()):  # "change my daily calories to 1950"
            self.fit.set_targets(**target)
            self.last_route = "target"
            card = coach.targets_card(self.fit)
            return [{**card, "text": f"Done: {coach.target_text(target)}."}]
        if coach.SUGGEST.search(question):
            self.last_route = "targets"
            profile = self.fit.profile()
            if not profile.get("weight_kg"):
                return [{"text": "To suggest targets I need your weight, height, age, how active you are and your goal: "
                                 "fill them in on the Fitness page and I'll work them out.", "kind": "text", "payload": None}]
            return [coach.targets_card(self.fit, suggested=suggest_targets(profile))]
        routed = route("fitness", question, previous) if self.settings.fast_answers else None
        explicit = (coach.FOOD_LOG.match(question) or coach.FOR_MEAL.match(question)
                    or coach.food_with_numbers(question))  # "2 eggs, 12g protein" isn't asking about your protein
        food = coach.food_request(question, own_chat=True) if routed is None or explicit else None
        if food:
            self.last_route = "food"
            return [self.food_reply(food)]
        return None

    def nutrition_reply(self, subject: str) -> dict:
        """A food's calories and macros (your numbers, the product's, or an estimate), with Log in case you had it."""
        card = self.food_reply(subject)
        if card["kind"] != "food":
            return card
        rows = self.fit.batch(card["payload"]["batch"])
        return {**card, "text": coach.nutrition_text(rows) + " Press Log if you had it."}

    def food_model(self):
        """The model that reads food and photos, started if need be; None if it can't be reached."""
        if self.food_backend is None:
            from doraemon.llm import get_backend, model_ready
            if not model_ready(self.settings, wait=20):
                return None
            self.food_backend = get_backend(self.settings.model, self.settings)
        return self.food_backend

    def read_food(self, text: str) -> list[dict] | None:
        """The foods in `text` with their numbers, logged nowhere; None if the model isn't there to read them."""
        if self.food_model() is None:
            return None
        return foodlog.understand(text, self.fit, self.db, self.food_backend)

    def photo(self, image: bytes, said: str = "", file: str | None = None) -> list[int]:
        """A photo you sent Dorae-3 (and what you said with it), and the answer: the message ids."""
        ids = [self.db.add_message("fitness", "user", said.strip()[:500], "photo", {"file": file} if file else None)]
        card = self.photo_reply(image, said)
        self.last_route = "photo"
        ids.append(self.db.add_message("fitness", "agent", card["text"], card["kind"], card["payload"], via="model"))
        return ids

    def photo_reply(self, image: bytes, said: str = "") -> dict:
        """A photo of a nutrition label or of the food itself, worked out as a draft card waiting for Log."""
        if self.food_model() is None:
            return {"text": "I need the local model to read photos, and it isn't running. Open the Ollama app and try "
                            "again.", "kind": "text", "payload": None}
        try:
            seen = foodlog.read_photo(image, said, self.fit, self.db, self.food_backend)
        except Exception as e:  # the model gave nothing usable, or can't see
            log.warning("reading a photo failed: %s", e)
            seen = {"kind": "failed"}
        if seen["kind"] == "label":
            batch = self.fit.draft(seen["foods"])
            self.db.set_setting(LABEL_KEY, json.dumps({"at": datetime.now(timezone.utc).isoformat(), "batch": batch,
                                                       "reading": seen["reading"]}))
            return {"text": self.label_text(seen["foods"][0]), "kind": "food", "payload": {"batch": batch}}
        if seen["kind"] == "food":
            foods = seen["foods"]
            batch = self.fit.draft(foods)
            return {"text": f"From your photo: about {sum(f['kcal'] for f in foods):,.0f} kcal and "
                            f"{sum(f['protein'] for f in foods):.0f} g protein. Photos give rough numbers: check them "
                            "(change any under Edit), then press Log.", "kind": "food", "payload": {"batch": batch}}
        text = {"unreadable": "I can see a nutrition label but couldn't read its numbers. Try again closer, straight on "
                              "and in good light.",
                "failed": "I couldn't read that photo just now. Try again, or tell me what you had."}.get(
            seen["kind"], "I couldn't see food or a nutrition label in that photo. Send one of your meal or of the "
                          "label's nutrition table.")
        return {"text": text, "kind": "text", "payload": None}

    @staticmethod
    def label_text(food: dict) -> str:
        return (f"From the label: {food['name']}, {food['portion']}: {food['kcal']:.0f} kcal, {food['protein']:.0f} g "
                "protein. Had a different amount? Tell me, like “330 ml”, “2 servings” or “half the pack”. Otherwise "
                "press Log.")

    def label_amount(self, text: str) -> dict | None:
        """"2 servings" right after a label photo: the label's food again, for that amount (the old draft goes)."""
        if (amount := foodlog.amount_reply(text)) is None:
            return None
        try:
            last = json.loads(self.db.get_setting(LABEL_KEY) or "null")
            fresh = datetime.now(timezone.utc) - datetime.fromisoformat(last["at"]) < timedelta(minutes=LABEL_MINUTES)
        except (ValueError, TypeError, KeyError):
            return None
        if not fresh:
            return None
        unit = foodlog.label_unit(last["reading"])
        if "size" in amount and unit not in (None, amount["size"][1]):
            return {"text": f"That label measures in {'grams' if unit == 'g' else 'ml'}: tell me "
                            f"{'grams, like “60 g”' if unit == 'g' else 'ml, like “330 ml”'}, or servings, like "
                            "“2 servings”.", "kind": "text", "payload": None}
        food = foodlog.label_food(last["reading"], text)
        if food is None:
            return None
        self.fit.cancel(last["batch"])  # a draft goes; one you already logged stays
        batch = self.fit.draft([food])
        self.db.set_setting(LABEL_KEY, json.dumps({**last, "batch": batch}))
        return {"text": self.label_text(food), "kind": "food", "payload": {"batch": batch}}

    def food_reply(self, text: str) -> dict:
        """Work out what you ate and save it as a draft card waiting for Log."""
        if self.food_model() is None:
            return {"text": "I need the local model to work out what you ate, and it isn't running. Open the Ollama "
                            "app and try again.", "kind": "text", "payload": None}
        try:
            foods = foodlog.understand(text, self.fit, self.db, self.food_backend)
        except Exception as e:  # the model gave nothing usable
            log.warning("reading food failed: %s", e)
            foods = []
        if not foods:
            return {"text": f"I couldn't work out the food in “{text}”. Try naming each thing, like “1 plate chicken "
                            "rice, 1 kopi siew dai”.", "kind": "text", "payload": None}
        batch = self.fit.draft(foods)
        kcal, protein = sum(f["kcal"] for f in foods), sum(f["protein"] for f in foods)
        return {"text": f"Here's what I worked out: {kcal:,.0f} kcal and {protein:.0f} g protein. Check the numbers "
                        "(change any under Edit), then press Log.", "kind": "food", "payload": {"batch": batch}}

    def reminder_reply(self, title: str, when: str, question: str) -> dict:
        """Draft the reminder you asked for as a card you can edit before pressing Create reminder."""
        result, item_id = draft_reminder(self.db, self.settings.timezone, self.settings.date_order, title, when,
                                         question, datetime.now(self.tz))
        if item_id is None:
            return {"text": result["error"], "kind": "text", "payload": None}
        if result.get("already_there"):
            status = self.db.get("item", item_id)["status"]
            return {"text": f"You already have that one: “{result['title']}”, {result['when']}.",
                    "kind": "items" if status == "proposed" else "agenda", "payload": {"ids": [item_id]}}
        return {"text": f"Here's your reminder: “{result['title']}”, {result['when']}. I'll remind you "
                        f"{result['reminds']} on Telegram. Change anything on the card, then press Create reminder.",
                "kind": "items", "payload": {"ids": [item_id]}}

    def calendar_reply(self, q: str) -> list[dict]:
        if (asked := split_request(q)) is not None:
            return [self.reminder_reply(*asked, q)]
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

    # --- net worth and income ----------------------------------------------------------------------

    def networth_card(self) -> dict:
        """Net worth now, its change since last month's end, by kind of account, and the monthly trend."""
        nw, home = self.nw, self.home
        ch = nw.change(self.today())
        now = ch["now"]
        if not now.counted:
            return {"text": "You haven't added any accounts yet. Add your bank accounts, CPF, investments and loans "
                            "on the Net worth page, and I'll track how the total changes each month.",
                    "kind": "text", "payload": None}
        months = nw.months(12, self.today())
        stale = [a["row"]["name"] for a in now.accounts if a["as_of"] and a["row"]["source"] == "manual"
                 and (self.today() - date.fromisoformat(a["as_of"])).days > 40]
        payload = {"currency": home, "total": str(now.total), "assets": str(now.assets), "liabilities": str(now.liabilities),
                   "change": str(ch["change"]) if ch["had_before"] else None, "pct": str(ch["pct"]) if ch["pct"] is not None else None,
                   "by_kind": [[k, KINDS[k], str(v)] for k, v in sorted(now.by_kind.items(), key=lambda kv: -kv[1])],
                   "months": [{"month": m["month"], "label": month_name(m["month"])[:3], "name": month_name(m["month"]),
                               "total": str(m["total"]), "partial": m["partial"]} for m in months],
                   "unconverted": now.unconverted, "stale": stale}
        text = f"Your net worth is {money(now.total, home)}"
        if ch["had_before"]:
            way = "up" if ch["change"] > 0 else "down" if ch["change"] < 0 else "unchanged"
            text += (f", {way} {money(abs(ch['change']), home)}" + (f" ({ch['pct']:+}%)" if ch["pct"] is not None else "")
                     + f" since the end of {month_name(shift_month(self.this_month(), -1))}." if way != "unchanged"
                     else f", the same as at the end of {month_name(shift_month(self.this_month(), -1))}.")
        else:
            text += ". Next month I can show how it changed."
        if stale:
            text += f" Not updated in over a month: {', '.join(stale[:3])}."
        if now.unconverted:
            text += f" Left out (no exchange rate yet): {', '.join(now.unconverted)}."
        return {"text": text, "kind": "networth", "payload": payload}

    def income_card(self, period: str | None = None) -> dict:
        """Income in a month against spending: what you kept."""
        first, last, label = self.span(period)
        month = (first or self.today()).strftime("%Y-%m")
        received = income.in_month(self.db, month, self.home, self.fx, self.today())
        got = income.total(received)
        spent = spending_totals(self.ledger(month), self.home, self.fx).total
        kept = got - spent
        if not received:
            return {"text": f"No income recorded {label_in(label)}. Add your salary (it can repeat monthly) on the "
                            "Income page.", "kind": "text", "payload": None}
        rate = int(kept / got * 100) if got > 0 else None
        text = (f"You received {money(got, self.home)} {label_in(label)} and spent {money(spent, self.home)}, so you "
                + (f"kept {money(kept, self.home)}" if kept >= 0 else f"spent {money(-kept, self.home)} more than came in")
                + (f" ({rate}% of your income)." if rate is not None and kept >= 0 else "."))
        return {"text": text, "kind": "income", "payload": {
            "month": month, "label": month_name(month), "currency": self.home, "income": str(got), "spent": str(spent),
            "kept": str(kept), "rows": [[r.label, CATEGORIES[r.category], str(r.home if r.home is not None else r.amount),
                                         r.day.isoformat(), r.monthly] for r in received]}}

    def holdings_card(self) -> dict:
        rows = self.nw.holdings()
        if not rows:
            return {"text": "No investments synced yet. Connect Webull (see the Net worth page) and I'll list what "
                            "you hold.", "kind": "text", "payload": None}
        total = sum((Decimal(r["market_value"]) for r in rows), Decimal(0))
        top = rows[0]
        return {"text": f"You hold {len(rows)} position{'s' if len(rows) != 1 else ''}; the biggest is {top['symbol']} "
                        f"({top['currency']} {Decimal(top['market_value']):,.2f}).",
                "kind": "holdings", "payload": {"ids": [[r["account_id"], r["symbol"]] for r in rows[:20]],
                                                "total": str(total), "currency": top["currency"]}}

    def networth_facts(self) -> dict:
        """For the model: the same numbers, worked out here."""
        card = self.networth_card()
        p = card["payload"]
        if p is None:
            return {"no_accounts": card["text"]}
        facts = {"summary": card["text"], "net_worth": f"{self.home} {Decimal(p['total']):,.2f}",
                 "by_kind": {label: f"{self.home} {Decimal(v):,.2f}" for _, label, v in p["by_kind"]},
                 "month_ends": {m["name"]: f"{self.home} {Decimal(m['total']):,.2f}" for m in p["months"]}}
        if p["change"] is not None:
            facts["change_since_last_month_end"] = f"{self.home} {Decimal(p['change']):+,.2f}" + (f" ({p['pct']}%)" if p["pct"] else "")
        return facts

    # --- instant answers (doraemon.router): everyday questions, straight from your data -----------

    def span(self, period: str | None, default: str | None = "this_month") -> tuple[date | None, date | None, str]:
        """(first day, last day, label) for a router period: 'this_week', 'month:9', or None for `default`."""
        if period and period.startswith("month:"):
            n = int(period.split(":")[1])
            year = self.today().year if n <= self.today().month else self.today().year - 1
            month = f"{year}-{n:02d}"
            return (*self.month_range(month), month_name(month))
        return self.period_range(period or default) or (None, None, "all time")

    def instant(self, route) -> dict:
        """The answer to a question the router recognised. Same cards as the model's answers."""
        s, home = route.slots, self.home
        if route.agent == "fitness" and route.intent.startswith("fit_"):
            return coach.answer(self.fit, route)
        if route.intent == "help":
            return {"text": HELP_TEXT[route.agent], "kind": "text", "payload": None}
        if route.intent == "find":
            return self.find_card(s["what"])
        if route.intent == "summary" or (route.intent == "how_was" and (s.get("period") or "").startswith(("month:", "last_month"))):
            first, _, _ = self.span(s.get("period"), "last_month")
            return self.monthly_summary(first.strftime("%Y-%m") if first else None)
        if route.intent in ("days", "day"):
            return self.days_card(*self.days_range(s.get("when") or s.get("day") or "today"))
        if route.intent in ("total", "payments"):
            # "did I have a 4.87 transaction": any time, unless you said when
            first, last, label = self.span(s.get("period"), None if s.get("amount") else "this_month")
            amount = Decimal(s["amount"]) if s.get("amount") else None
            kept = [t for t in self.ledger_between(first, last)
                    if (not s.get("category") or t.category.value == s["category"])
                    and (not s.get("merchant") or s["merchant"] in t.merchant.lower())
                    and (amount is None or t.amount == amount)]
            what = (f" of {amount:,.2f}" if amount is not None else "") + (f" on {s['category']}" if s.get("category") else "") \
                + (f" at {s['merchant'].title()}" if s.get("merchant") else "")
            if not kept:
                return {"text": f"No payments{what} {label_in(label)}.", "kind": "text", "payload": None}
            total = spending_totals(kept, home, self.fx).total
            if amount is not None:
                t = kept[-1]
                day = t.purchased_at.astimezone(self.tz)
                text = (f"Yes: {t.merchant}, {t.currency} {t.amount:,.2f} on {day:%a} {day.day} {day:%b}"
                        + (f", and {len(kept) - 1} more of the same amount." if len(kept) > 1 else "."))
            else:
                text = (f"You spent {money(total, home)}{what} {label_in(label)} across {len(kept)} "
                        f"payment{'s' if len(kept) != 1 else ''}.")
            if s.get("category") or s.get("merchant") or route.intent == "payments":
                kept.sort(key=lambda t: t.purchased_at, reverse=True)
                return {"text": text + (" Showing the latest 20." if len(kept) > 20 else ""), "kind": "payments",
                        "payload": {"ids": [int(t.message_id) for t in kept[:20]]}}
            card = self.summary(kept, label[0].upper() + label[1:])
            card["text"] = text
            return card
        if route.intent in ("breakdown", "how_was"):
            first, last, label = self.span(s.get("period"))
            card = self.summary(self.ledger_between(first, last), label[0].upper() + label[1:])
            p = card["payload"]
            if not p["count"]:
                return {"text": f"No payments {label_in(label)}.", "kind": "text", "payload": None}
            top = p["rows"][0][0] if p["rows"] else ""
            card["text"] = (f"{label[0].upper() + label[1:]}: {money(Decimal(p['total']), home)} across {p['count']} "
                            f"payments" + (f", mostly {top}." if top else "."))
            return card
        if route.intent in ("latest", "biggest"):
            first, last, label = self.span(s.get("period"), None)
            kept = self.ledger_between(first, last)
            if not kept:
                return {"text": f"No payments {label_in(label)}.", "kind": "text", "payload": None}
            sums = spending_totals(kept, home, self.fx)
            if route.intent == "biggest":
                kept.sort(key=lambda t: sums.home_amounts.get(id(t), t.amount), reverse=True)
            else:
                kept.sort(key=lambda t: t.purchased_at, reverse=True)
            t = kept[0]
            day = t.purchased_at.astimezone(self.tz)
            which = "Your biggest purchase" + (f" {label_in(label)}" if s.get("period") else "") \
                if route.intent == "biggest" else "Your latest purchase"
            return {"text": f"{which}: {t.merchant}, {t.currency} {t.amount:,.2f} on {day:%a} {day.day} {day:%b}.",
                    "kind": "payments", "payload": {"ids": [int(x.message_id) for x in kept[:5]]}}
        if route.intent == "top":
            first, last, label = self.span(s.get("period"))
            return self.top_merchants_of(self.ledger_between(first, last), label)
        if route.intent == "budget":
            return self.budget_card()
        if route.intent == "networth":
            return self.networth_card()
        if route.intent == "income":
            return self.income_card(s.get("period"))
        if route.intent == "holdings":
            return self.holdings_card()
        if route.intent == "trend":
            return self.trend(category=s.get("category"))
        if route.intent == "week":
            return self.week("next week" if s.get("which") == "next" else "this week")
        if route.intent == "reminders":
            return self.reminders_card()
        question = {"waiting": "what needs my ok", "day": f"what's on {s.get('day', 'today')}",
                    "due": "what's due this week", "trips": "my trips", "passed": "what already passed"}[route.intent]
        return self.calendar_reply(question)[0]

    def reminders_card(self) -> dict:
        """Your items with Telegram reminders still to come, the next one first."""
        nxt: dict[int, datetime] = {}
        for r in self.db.upcoming_reminders():
            nxt.setdefault(r["item_id"], datetime.fromisoformat(r["remind_at"]))
        if not nxt:
            return {"text": "No reminders set. Ask me (\"remind me to get groceries tomorrow\"), or press Remind me "
                            "on a bill, deadline or delivery.", "kind": "text", "payload": None}
        ids = sorted(nxt, key=nxt.get)
        first = self.db.get("item", ids[0])
        at = short_time(nxt[ids[0]], self.tz)
        return {"text": f"{len(ids)} thing{'s' if len(ids) != 1 else ''} with reminders on Telegram. "
                        f"Next: “{first['title']}” on {at}.", "kind": "agenda", "payload": {"ids": ids[:20]}}

    # --- shared --------------------------------------------------------------

    def ensure_overview(self, agent: str, scheduled: bool = False) -> list[dict]:
        """Post today's overview once a day, so opening the chat shows where things stand. Dorae-2's is the
        briefing: before its time, opening the chat waits for it (unless the chat is empty, e.g. a new chat).
        Returns the messages posted (none if it had already gone out)."""
        key = f"overview:{agent}"
        with self.overview_lock:
            today = self.today().isoformat()
            if self.db.get_setting(key) == today:
                return []
            at = self.briefing_time()
            if (agent == "calendar" and not scheduled and at is not None and datetime.now(self.tz).time() < at
                    and self.db.last_message(agent) is not None):
                return []
            msgs = (self.money_overview() if agent == "money" else self.briefing() if agent == "calendar"
                    else [coach.day_card(self.fit, self.today(), f"Today, {coach.short_day(self.today())}")])
            for msg in msgs:
                self.db.add_message(agent, "agent", msg["text"], msg["kind"], msg["payload"])
            self.db.set_setting(key, today)
            return msgs

    def reply(self, agent: str, question: str) -> list[int]:
        """Store your question and the agent's answer; returns the new message ids.

        The model answers when one is set (self.chat, see doraemon.chat); if it can't be
        reached, the fixed keyword answers below take over.
        """
        history = self.db.messages(agent, limit=8)
        previous = next((r["text"] for r in reversed(history) if r["role"] == "user"), None)
        ids = [self.db.add_message(agent, "user", question)]
        answers = None
        self.last_route = "model"
        if agent == "fitness":  # Dorae-3: targets you set and food you log come before anything else
            answers = self.fitness_first(question, previous, history)
        asked = split_request(question) if agent == "calendar" else None
        event = split_event(question)
        budget = bud.parse_request(question) if agent == "money" else None
        fast = route(agent, question, previous) if self.settings.fast_answers else None
        if answers is not None:
            pass
        elif asked and asked[0] and asked[1]:  # "remind me to X tomorrow": clear enough to draft without the model
            answers, self.last_route = [self.reminder_reply(*asked, question)], "reminder"
        elif event and event.title and event.when and not budget:  # "add date night on 26/10 7-9pm": likewise
            self.last_route = "event"
            answers = [self.event_reply(event, question)] if agent == "calendar" else [
                {"text": f"That's one for {styled(CALENDAR, self.db).name}. Open {styled(CALENDAR, self.db).name}'s chat "
                         "and ask there.", "kind": "text", "payload": None}]
        elif budget:  # "set my dining budget to 300": drafted as a card, no model needed
            answers, self.last_route = [self.budget_card(draft=budget)], "budget draft"
        elif fast is not None:  # an everyday lookup: answered from the data in milliseconds
            self.last_route = fast.intent
            if fast.agent == agent:
                answers = [self.instant(fast)]
            else:
                other = styled(AGENTS[fast.agent], self.db)
                answers = [{"text": f"That's one for {other.name}. Open {other.name}'s chat and ask there.",
                            "kind": "text", "payload": None}]
        elif self.chat is not None:
            try:
                answers = self.chat.answer(agent, question, history)
            except Exception as e:  # model not running, timed out, or gave nothing back
                log.warning("chat model failed, using fixed answers: %s", e)
        if answers is None:
            answers = self.money_reply(question) if agent == "money" else self.calendar_reply(question) \
                if agent == "calendar" else [coach.answer(self.fit, Route("fitness", "fit_day"))]
            if self.chat is not None:
                answers[0]["text"] += "\n(Quick answer: the local model isn't responding right now.)"
        for msg in answers:  # `via` puts a ⚡ on answers that never waited for the model
            ids.append(self.db.add_message(agent, "agent", msg["text"], msg["kind"], msg["payload"], via=self.last_route))
        return ids
