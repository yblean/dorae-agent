"""Plain messages to the Telegram bot, so your phone can do more than receive reminders.

- "remind me to call mom at 5pm tmr" is drafted as in Dorae-2's chat, with Create and Cancel
  buttons. Without a date, the bot asks when and reads your next message as the answer.
- The everyday questions the fast path knows (doraemon.router), from either agent: "how much did I
  spend this week?", "what's on tomorrow?", "my budget", "and last month?". Answered from your
  data in milliseconds, as plain text (titles come from email, so no formatting).

Anything else gets examples of what works here. The model only answers in the app: on the phone
an answer should be instant or not at all.
"""
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon.agents import Brain, money, short_clock
from doraemon.db import item_model, transaction_model
from doraemon.reminders import defaults_for, draft_reminder, plan, short_time, split_request, when_line
from doraemon.router import route

log = logging.getLogger(__name__)

ASK_KEY = "telegram:asking"            # {"title", "at"}: the bot asked when to remind you about this
LAST_KEY = "telegram:last_question"    # for follow-ups like "and last month?"
ASK_MINUTES = 15                       # an answer to "when?" counts for this long
LIST = 8                               # lines under an answer; the rest are in the app
EXAMPLES = ("remind me to call mom at 5pm tmr", "how much did I spend this week?", "what's on tomorrow?",
            "my budget", "my reminders")

Buttons = list[list[tuple[str, str]]]


class Phone:
    def __init__(self, brain: Brain, help_text=lambda: "") -> None:
        self.brain, self.db, self.settings = brain, brain.db, brain.settings
        self.tz = ZoneInfo(self.settings.timezone)
        self.help_text = help_text  # () -> the bot's command list, for messages it doesn't understand

    def answer(self, text: str, now: datetime | None = None) -> tuple[str, Buttons | None]:
        """Your message -> (reply, buttons). Never raises: a failure is said, not lost."""
        try:
            return self._answer(text, now or datetime.now(self.tz))
        except Exception:
            log.exception("answering a Telegram message failed")
            return "Sorry, something went wrong answering that. Try again, or ask in the Doraemon app.", None

    def _answer(self, text: str, now: datetime) -> tuple[str, Buttons | None]:
        asking = self.asked_about(now)
        self.db.set_setting(ASK_KEY, None)
        if (asked := split_request(text)) is not None:
            return self.draft(*asked, text, now)
        found = route("money", text, self.db.get_setting(LAST_KEY))  # tries Dorae-2's questions too
        if found is not None:
            self.db.set_setting(LAST_KEY, text)
            return self.plain(self.brain.instant(found)), None
        if asking:  # "When should I remind you to buy milk?" -> "tomorrow 6pm"
            return self.draft(asking, text.strip(), f"remind me to {asking} {text.strip()}", now)
        return self.help_text(), None

    def asked_about(self, now: datetime) -> str | None:
        """The title of the reminder the bot just asked "when?" about, if it's still a fresh question."""
        try:
            asked = json.loads(self.db.get_setting(ASK_KEY) or "null")
            at = datetime.fromisoformat(asked["at"])
        except (ValueError, TypeError, KeyError):
            return None
        return asked["title"] if now - at < timedelta(minutes=ASK_MINUTES) else None

    # --- reminders ------------------------------------------------------------------------------

    def draft(self, title: str, when: str, question: str, now: datetime) -> tuple[str, Buttons | None]:
        result, item_id = draft_reminder(self.db, self.settings.timezone, self.settings.date_order, title, when,
                                         question, now)
        if item_id is None:
            if title:  # the date was missing, unreadable or past: your next message can be the answer
                self.db.set_setting(ASK_KEY, json.dumps({"title": title, "at": now.isoformat()}))
            return result["error"], None
        row = self.db.get("item", item_id)
        item = item_model(row)
        if result.get("already_there") and row["status"] == "confirmed":
            return f"You already have that reminder: {row['title']}, {when_line(item, self.settings.timezone, now)}.", None
        return (f"📝 New reminder: {row['title']}\n{when_line(item, self.settings.timezone, now)}\n\n"
                f"I'll remind you {result.get('reminds') or self.reminds(item, now)}."), \
            [[("✅ Create", f"make:{item_id}"), ("✖ Cancel", f"drop:{item_id}")]]

    def reminds(self, item, now: datetime) -> str:
        first, times = defaults_for(item.type)
        planned, _ = plan(item, self.settings.timezone, first, times, now)
        return ", ".join(f"{short_time(r['at'], self.tz)} ({r['label']})" for r in planned)

    def create(self, item_id: int, now: datetime | None = None) -> str:
        """You pressed Create under a drafted reminder: confirm it with its reminders, as the app's button does."""
        now = now or datetime.now(self.tz)
        row = self.db.get("item", item_id)
        if row is None or row["status"] == "dismissed":
            return "That reminder was cancelled. Send it to me again to make a new one."
        if row["status"] == "confirmed":
            return "✅ Already created."
        item = item_model(row)
        first, times = defaults_for(row["type"])
        planned, _ = plan(item, self.settings.timezone, first, times, now)
        if not planned:
            return "That time has already passed. Send me the reminder again with a new time."
        self.db.set_reminders(item_id, planned)
        self.db.update("item", item_id, "confirm", status="confirmed")
        whens = ", ".join(short_time(r["at"], self.tz) for r in planned)
        self.db.add_message("calendar", "agent", f"🔔 Reminder created on Telegram: “{row['title']}”, "
                                                 f"{when_line(item, self.settings.timezone, now)}.")
        return f"✅ Created. I'll remind you {whens}."

    def cancel(self, item_id: int) -> str:
        row = self.db.get("item", item_id)
        if row is None or row["status"] == "dismissed":
            return "✖ Cancelled."
        if row["status"] == "confirmed":
            return "That one's already created. Press Done when it comes, or change it under Upcoming in Doraemon."
        self.db.update("item", item_id, "dismiss", status="dismissed")
        return "✖ Cancelled."

    # --- answers as plain text ---------------------------------------------------------------------

    def plain(self, card: dict) -> str:
        """An answer card (doraemon.agents) as text: its sentence, then a few lines of what the card shows."""
        text, kind, p = card["text"].replace(" (dashed)", ""), card["kind"], card["payload"]
        if not p:
            return text
        cur = p.get("currency") or self.brain.home
        lines: list[str] = []
        ids = p.get("ids") or []
        if kind == "payments":
            for row in filter(None, (self.db.get("transaction", i) for i in ids[:LIST])):
                t = transaction_model(row)
                d = t.purchased_at.astimezone(self.tz)
                lines.append(f"• {d:%a} {d.day} {d:%b}  {t.merchant}  {t.currency} {t.amount:,.2f}")
        elif kind in ("items", "agenda"):
            for row in filter(None, (self.db.get("item", i) for i in ids[:LIST])):
                lines.append(f"• {row['title']} · {self.day(row)}"
                             + (f" · {row['currency']} {row['amount']}" if row["amount"] else ""))
        elif kind == "breakdown" and "\n" not in text:  # top merchants already list themselves
            lines = [f"• {c.capitalize()}: {money(Decimal(a), cur)}" for c, a in p["rows"][:LIST]]
        elif kind == "budgets" and len(p["rows"]) > 1:  # one budget: the sentence already says it all
            lines = [f"• {r['label']}: {money(Decimal(r['spent']), cur)} of {money(Decimal(r['limit']), cur)} ({r['used']}%)"
                     for r in p["rows"]]
        elif kind == "trend":
            lines = [f"• {m['label']}{' (so far)' if m['partial'] else ''}: {money(Decimal(m['total']), cur)}"
                     for m in p["months"]]
        elif kind == "networth":
            lines = [f"• {label}: {money(Decimal(v), cur)}" for _, label, v in p["by_kind"]]
        elif kind == "holdings":
            held = {(h["account_id"], h["symbol"]): h for h in self.brain.nw.holdings()}
            lines = [f"• {h['symbol']}: {h['currency']} {Decimal(h['market_value']):,.2f}"
                     for h in (held.get(tuple(k)) for k in ids[:LIST]) if h]
        elif kind == "week":
            lines = self.week_lines(p["events"])
        more = (len(ids) - LIST) + p.get("more", 0) if kind in ("payments", "items", "agenda", "holdings") else 0
        if more > 0:
            lines.append(f"…and {more} more in the Doraemon app.")
        if kind == "items":
            lines.append("Confirm or dismiss them in the Doraemon app.")
        return text + ("\n\n" + "\n".join(lines) if lines else "")

    def day(self, row) -> str:
        """'Wed 14 Oct' or 'Wed 14 Oct, 5pm' for an item."""
        item = item_model(row)
        if item.start_at is None:
            return "no date"
        if item.all_day:
            d = item.start_at.astimezone(ZoneInfo(item.timezone))
            return f"{d:%a} {d.day} {d:%b}"
        return short_time(item.start_at, self.tz)

    def week_lines(self, events: list[dict]) -> list[str]:
        """Each day with events, then its events (an event spanning days shows on its first day)."""
        lines, last = [], None
        for e in events:
            if e["first"] != last:
                d = datetime.fromisoformat(e["first"])
                lines += ([""] if last else []) + [f"{d:%a} {d.day} {d:%b}"]
                last = e["first"]
            when = short_clock(e["start"]) if e["start"] else "All day"
            lines.append(f"• {when}  {e['title']}" + (" (needs your OK)" if e.get("waiting") else ""))
        return lines

