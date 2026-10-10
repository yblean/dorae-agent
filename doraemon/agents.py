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
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from doraemon import budgets as bud
from doraemon.assistant import last_full_month, month_name, spending as spending_text
from doraemon.calendar_sync import ALL_DAY_TYPES, Schedule
from doraemon.config import Settings
from doraemon.db import Database, from_chat, item_model, transaction_model
from doraemon.display import describe_when
from doraemon.ledger import build_ledger, spending_totals
from doraemon.reminders import draft_reminder, split_request
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
    ("What's my latest purchase?", "Show this month's breakdown", "How am I doing on my budget?",
     "Is my spending going up or down?", "How was {last_month}?"),
    (("Check inbox for receipts", "With Dorae-2's check", "On"),
     ("Budget alerts", "At 80% and 100% of a budget", "Budgets"),
     ("Monthly summary", "1st of each month, 9:00", "Coming soon")),
)
CALENDAR = Agent(
    "calendar", "Dorae-2", "Calendar & reminders", "#EC4899",
    ("What needs my OK?", "Show my schedule this week", "What's due this week?", "My upcoming trips"),
    (("Check inbox", "Every 15 minutes", "Run now"),
     ("Morning briefing", "Every day", "Briefing"),
     ("Telegram reminders", "Bills, deadlines, deliveries, RSVPs", "Telegram")),
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
        card["text"] += extra
        overall = next((s for s in self.budget_statuses() if s.category == bud.TOTAL), None)
        if overall:
            card["text"] += " " + bud.describe(overall, self.home)
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
                and (not types or r["type"] in types)]
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
        for r in self.db.items(("proposed", "confirmed")):
            day = self._dated(r)
            if r["calendar_event_id"] or day is None or not first <= day <= last:
                continue  # already in Google Calendar, or not in these days
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

    def morning_tick(self, now: datetime | None = None) -> bool:
        """Post the briefing once it's time and it hasn't gone out today. Returns whether it was posted."""
        at = self.briefing_time()
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        if at is None or now.time() < at:
            return False
        return self.ensure_overview("calendar", scheduled=True)

    def run_briefing(self, seconds: int = 60) -> threading.Event:
        """Check every minute whether the briefing is due, while the web app runs. Set the event to stop."""
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(seconds):
                try:
                    self.morning_tick()
                except Exception:  # keep the loop alive; the next minute tries again
                    log.exception("posting the briefing failed")
        threading.Thread(target=loop, daemon=True, name="briefing").start()
        return stop

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

    # --- shared --------------------------------------------------------------

    def ensure_overview(self, agent: str, scheduled: bool = False) -> bool:
        """Post today's overview once a day, so opening the chat shows where things stand. Dorae-2's is the
        briefing: before its time, opening the chat waits for it (unless the chat is empty, e.g. a new chat).
        Returns whether one was posted."""
        key = f"overview:{agent}"
        with self.overview_lock:
            today = self.today().isoformat()
            if self.db.get_setting(key) == today:
                return False
            at = self.briefing_time()
            if (agent == "calendar" and not scheduled and at is not None and datetime.now(self.tz).time() < at
                    and self.db.last_message(agent) is not None):
                return False
            for msg in (self.money_overview() if agent == "money" else self.briefing()):
                self.db.add_message(agent, "agent", msg["text"], msg["kind"], msg["payload"])
            self.db.set_setting(key, today)
            return True

    def reply(self, agent: str, question: str) -> list[int]:
        """Store your question and the agent's answer; returns the new message ids.

        The model answers when one is set (self.chat, see doraemon.chat); if it can't be
        reached, the fixed keyword answers below take over.
        """
        history = self.db.messages(agent, limit=8)
        ids = [self.db.add_message(agent, "user", question)]
        answers = None
        asked = split_request(question) if agent == "calendar" else None
        if asked and asked[0] and asked[1]:  # "remind me to X tomorrow": clear enough to draft without the model
            answers = [self.reminder_reply(*asked, question)]
        elif self.chat is not None:
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
