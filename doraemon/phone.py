"""Plain messages to the Telegram bot, so your phone can do more than receive reminders.

- "remind me to call mom at 5pm tmr" is drafted as in Dorae-2's chat, with Create and Cancel
  buttons. Without a date, the bot asks when and reads your next message as the answer.
- "add date night on 26/10 7-9pm" drafts a calendar event (doraemon.events), with Add to calendar
  and Cancel buttons; Add puts it in your Doraemon Google Calendar. It asks when the same way.
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

from doraemon import coach, senders
from doraemon.agents import Brain, money, short_clock
from doraemon.calendar_sync import add_to_calendar
from doraemon.db import item_model, transaction_model
from doraemon.events import draft_event, split_event
from doraemon.reminders import (defaults_for, draft_reminder, plan, short_time, split_request, wants_reminder,
                                when_line)
from doraemon.router import route

log = logging.getLogger(__name__)

ASK_KEY = "telegram:asking"            # {"title", "kind", "at"}: the bot asked when this reminder or event is
LAST_KEY = "telegram:last_question"    # for follow-ups like "and last month?"
ASK_MINUTES = 15                       # an answer to "when?" counts for this long
LIST = 8                               # lines under an answer; the rest are in the app
EXAMPLES = ("remind me to call mom at 5pm tmr", "add dinner with Sam on friday 7-9pm",
            "how much did I spend this week?", "what's on tomorrow?", "my budget", "my reminders",
            "ate chicken rice and kopi siew dai", "how am I doing today?")

Buttons = list[list[tuple[str, str]]]


class Phone:
    def __init__(self, brain: Brain, calendar=lambda: None, help_text=lambda: "") -> None:
        self.brain, self.db, self.settings = brain, brain.db, brain.settings
        self.calendar = calendar  # () -> the Doraemon calendar, or None until Google Calendar is connected
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
        if (event := split_event(text)) is not None:
            return self.event(event.title, event.when, event.end, text, now)
        if target := coach.parse_target(text):  # "set my protein target to 140"
            self.brain.fit.set_targets(**target)
            return self.plain(coach.targets_card(self.brain.fit)), None
        if food := coach.food_request(text, own_chat=False):  # "ate chicken rice", "lunch: laksa"
            return self.food(food)
        # Dorae-3's own shapes first ("how am I doing today?"), then everyone else's
        found = route("fitness", text, self.db.get_setting(LAST_KEY))
        if found is not None and found.intent == "help":
            return self.help_text(), None  # both agents' examples and the commands
        if found is not None:
            self.db.set_setting(LAST_KEY, text)
            return self.plain(self.brain.instant(found)), None
        if asking and asking.get("kind") == "event":  # "When is “Dinner with Sam”?" -> "friday 7-9pm"
            event = split_event(f"event: {asking['title']} {text.strip()}")
            return self.event(asking["title"], event.when if event else text.strip(), event.end if event else "",
                              text, now)
        if asking:  # "When should I remind you to buy milk?" -> "tomorrow 6pm"
            return self.draft(asking["title"], text.strip(), f"remind me to {asking['title']} {text.strip()}", now)
        return self.help_text(), None

    def food(self, text: str) -> tuple[str, Buttons | None]:
        """What you ate, worked out (the model, your foods, Open Food Facts) as a draft with Log and Cancel."""
        card = self.brain.food_reply(text)
        if card["kind"] != "food":
            return card["text"], None
        batch = card["payload"]["batch"]
        lines = ["🍽 Here's what I worked out:", *coach.food_lines(self.brain.fit.batch(batch)),
                 "", "Numbers off? Log it, then fix them in Dorae-3's chat (Edit): I'll remember them."]
        return "\n".join(lines), [[("✅ Log", f"flog:{batch}"), ("✖ Cancel", f"fdrop:{batch}")]]

    def log_food(self, batch: str) -> str:
        rows = self.brain.fit.log(batch)
        if not rows:
            return "That one's already logged (or was cancelled)."
        s = self.brain.fit.day_status()
        t, goals = s["totals"], s["targets"]
        self.db.add_message("fitness", "agent", f"🍽 Logged on Telegram: " + ", ".join(r["name"] for r in rows)
                            + f" ({sum(r['kcal'] for r in rows):,.0f} kcal).")
        return (f"✅ Logged. Today: {t.kcal:,.0f}" + (f" of {goals['kcal']:,}" if goals["kcal"] else "") + " kcal, "
                f"{t.protein:.0f}" + (f" of {goals['protein']}" if goals["protein"] else "") + " g protein.")

    def asked_about(self, now: datetime) -> dict | None:
        """{"title", "kind"} of the reminder or event the bot just asked "when?" about, if it's still a fresh question."""
        try:
            asked = json.loads(self.db.get_setting(ASK_KEY) or "null")
            at = datetime.fromisoformat(asked["at"])
        except (ValueError, TypeError, KeyError):
            return None
        return asked if now - at < timedelta(minutes=ASK_MINUTES) else None

    # --- reminders ------------------------------------------------------------------------------

    def draft(self, title: str, when: str, question: str, now: datetime) -> tuple[str, Buttons | None]:
        result, item_id = draft_reminder(self.db, self.settings.timezone, self.settings.date_order, title, when,
                                         question, now)
        if item_id is None:
            if title:  # the date was missing, unreadable or past: your next message can be the answer
                self.db.set_setting(ASK_KEY, json.dumps({"title": title, "kind": "reminder", "at": now.isoformat()}))
            return result["error"], None
        row = self.db.get("item", item_id)
        item = item_model(row)
        if result.get("already_there") and row["status"] == "confirmed":
            return f"You already have that reminder: {row['title']}, {when_line(item, self.settings.timezone, now)}.", None
        return (f"📝 New reminder: {row['title']}\n{when_line(item, self.settings.timezone, now)}\n\n"
                f"I'll remind you {result.get('reminds') or self.reminds(item, now)}."), \
            [[("✅ Create", f"make:{item_id}"), ("✖ Cancel", f"drop:{item_id}")]]

    def event(self, title: str, when: str, end: str, question: str, now: datetime) -> tuple[str, Buttons | None]:
        result, item_id = draft_event(self.db, self.settings.timezone, self.settings.date_order, title, when,
                                      question, now, end=end)
        if item_id is None:
            if title:  # the date was missing, unreadable or past: your next message can be the answer
                self.db.set_setting(ASK_KEY, json.dumps({"title": title, "kind": "event", "at": now.isoformat()}))
            return result["error"], None
        row = self.db.get("item", item_id)
        if result.get("already_there") and row["status"] == "confirmed":
            return f"You already have that: {row['title']}, {self.event_when(row)}.", None
        return (f"📅 New event: {row['title']}\n{self.event_when(row)}\n\n"
                "Press Add to put it in your Doraemon calendar."), \
            [[("✅ Add to calendar", f"make:{item_id}"), ("✖ Cancel", f"drop:{item_id}")]]

    def event_when(self, row) -> str:
        """'Mon 26 Oct, 7pm – 9pm', or 'Mon 26 Oct (all day)'."""
        item = item_model(row)
        if item.all_day:
            last = item.end_at.astimezone(ZoneInfo(item.timezone)) if item.end_at else None
            return f"{self.day(row)}" + (f" – {last:%a} {last.day} {last:%b}" if last else "") + " (all day)"
        text = short_time(item.start_at, self.tz)
        if item.end_at:
            end = short_time(item.end_at, self.tz)
            same_day = item.end_at.astimezone(self.tz).date() == item.start_at.astimezone(self.tz).date()
            text += f" – {end.split(', ')[1] if same_day else end}"
        return text

    def reminds(self, item, now: datetime) -> str:
        first, times = defaults_for(item.type)
        planned, _ = plan(item, self.settings.timezone, first, times, now)
        return ", ".join(f"{short_time(r['at'], self.tz)} ({r['label']})" for r in planned)

    def create(self, item_id: int, now: datetime | None = None) -> str:
        """You pressed Create under a drafted reminder (confirm it with its reminders) or Add under a drafted event
        (confirm it and put it in Google Calendar), as the app's buttons do."""
        now = now or datetime.now(self.tz)
        row = self.db.get("item", item_id)
        if row is None or row["status"] == "dismissed":
            return "That was cancelled. Send it to me again to make a new one."
        if not wants_reminder(row["type"]):
            return self.add_event(row)
        if row["status"] == "confirmed":
            return "✅ Already created."
        item = item_model(row)
        if item.start_at is None:
            return "It has no date, so I can't time a reminder: set one under Edit in Doraemon."
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

    def new_item(self, row) -> tuple[str, Buttons]:
        """A suggestion just found in your email, to approve from your phone: Remind me (bills, deadlines, deliveries,
        RSVPs) or Confirm (into Google Calendar), and Dismiss. The same as the buttons in the app."""
        kind = (row["type"] or "").replace("_", " ").capitalize()
        when = self.day(row) + (f" · {row['currency']} {row['amount']}" if row["amount"] else "") \
            + (f" · {row['location']}" if row["location"] else "")
        sender = senders.name(row["sender"] or "")
        lines = [f"📬 New from your email: {row['title']}", f"{kind} · {when}"]
        if sender:
            lines.append(f"From {sender}")
        if row["evidence_snippet"]:
            lines.append(f"“{row['evidence_snippet'][:140]}”")
        ok = ("🔔 Remind me", f"make:{row['id']}") if wants_reminder(row["type"]) else ("✅ Confirm", f"make:{row['id']}")
        return "\n".join(lines), [[ok, ("✖ Dismiss", f"drop:{row['id']}")]]

    def add_event(self, row) -> str:
        if row["status"] == "confirmed" and row["calendar_event_id"]:
            return "✅ Already in your calendar."
        if row["status"] != "confirmed":
            self.db.update("item", row["id"], "confirm", status="confirmed")
        said = add_to_calendar(self.db, self.calendar(), row["id"])
        self.db.add_message("calendar", "agent", f"{said} (You added it on Telegram.)")
        return said

    def cancel(self, item_id: int) -> str:
        row = self.db.get("item", item_id)
        if row is None or row["status"] == "dismissed":
            return "✖ Cancelled."
        if row["status"] == "confirmed":
            return ("That one's already created. Press Done when it comes, or change it under Upcoming in Doraemon."
                    if wants_reminder(row["type"]) else
                    "That one's already in your calendar. Change or remove it under Upcoming in Doraemon.")
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
        elif kind == "fitness_day":
            rows = []
            for key, label, unit in (("kcal", "Calories", " kcal"), ("protein", "Protein", " g")):
                goal = p["targets"].get(key)
                rows.append(f"• {label}: {p['eaten'][key]:,}" + (f" of {goal:,}" if goal else "") + unit)
            if p["steps"] is not None:
                rows.append(f"• Steps: {p['steps']:,}" + (f" of {p['targets']['steps']:,}" if p["targets"].get("steps") else ""))
            lines = rows + ([""] + [f"– {f[1]}: {f[3]} kcal, {f[4]} g" for f in p["foods"][:LIST]] if p["foods"] else [])
        elif kind == "fitness_week":
            lines = [f"• {r['name']}: " + (f"{r['kcal']:,} kcal, {r['protein']} g" if r["foods"] else "not logged")
                     + (f", {r['steps']:,} steps" if r["steps"] is not None else "") for r in p["rows"]]
        elif kind == "days":
            for d in p["days"]:
                lines += ([""] if lines else []) + [d["label"]]
                lines += [f"• {short_clock(e['start']) if e['start'] and e['first'] == d['date'] else 'All day'}  "
                          f"{e['title']}" + (" (needs your OK)" if e.get("waiting") else "") for e in d["events"]] or ["• Free"]
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

