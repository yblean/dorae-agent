"""Plain messages to the Telegram bot, so your phone can do more than receive reminders.

- "remind me to call mom at 5pm tmr" is drafted as in Dorae-2's chat, with Create and Cancel
  buttons. Without a date, the bot asks when and reads your next message as the answer.
- "add date night on 26/10 7-9pm" drafts a calendar event (doraemon.events), with Add to calendar
  and Cancel buttons; Add puts it in your Doraemon Google Calendar. It asks when the same way.
- The everyday questions the fast path knows (doraemon.router), from either agent: "how much did I
  spend this week?", "what's on tomorrow?", "my budget", "and last month?". Answered from your
  data in milliseconds, as plain text (titles come from email, so no formatting).
- Food for Dorae-3: "ate chicken rice", "a protein shake", "same as yesterday", or a plain "laksa" (the model says
  whether it's food), as a draft with Log, Edit and Cancel. Food sent while the bot was off is still logged, on the
  day you sent it.

Anything else gets examples of what works here. The model only answers in the app: on the phone
an answer should be instant or not at all.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon import coach, nudges, senders
from doraemon.agents import Brain, money, short_clock
from doraemon.calendar_sync import add_to_calendar
from doraemon.db import item_model, transaction_model
from doraemon.events import draft_event, split_event
from doraemon.fitness import key_of
from doraemon.foodlog import edit_numbers
from doraemon.reminders import (defaults_for, draft_reminder, plan, short_time, split_request, wants_reminder,
                                when_line)
from doraemon.router import route

log = logging.getLogger(__name__)

ASK_KEY = "telegram:asking"            # {"title", "kind", "at"}: the bot asked when this reminder or event is
LAST_KEY = "telegram:last_question"    # for follow-ups like "and last month?"
ASK_MINUTES = 15                       # an answer to "when?" counts for this long
LATE = timedelta(minutes=10)           # a message older than this was sent while the bot was off
MODEL_DOWN = "I need the local model"  # how Brain.food_reply starts when Ollama can't be reached
# A photo's caption saying it's a booking screenshot (for Dorae-2's calendar, not a meal)
BOOKING_HINT = re.compile(r"\b(?:book(?:ing|ed)?|hotels?|hostel|homestay|airbnb|stay|check[- ]?in|trains?|tickets?|"
                          r"flights?|bus|ferry|trip|travel|reservation|appointment|calendar|itinerary|schedule|event)\b",
                          re.I)
EDIT_KEY = "telegram:editing_food"     # {"batch", "at"}: Edit was pressed under this food
NEW_FOOD = re.compile(r"\s*(?:i\s+)?(?:just\s+)?(?:had|ate|eaten|drank|breakfast|lunch|dinner|supper|snack)\b", re.I)
LIST = 8                               # lines under an answer; the rest are in the app
EXAMPLES = ("remind me to call mom at 5pm tmr", "add dinner with Sam on friday 7-9pm",
            "how much did I spend this week?", "what's on tomorrow?", "my budget", "my reminders",
            "ate chicken rice and kopi siew dai", "same lunch as yesterday",
            "a photo of a nutrition label (caption: 2 servings)",
            "how am I doing today?")

Buttons = list[list[tuple[str, str]]]


def food_buttons(batch: str) -> Buttons:
    return [[("✅ Log", f"flog:{batch}"), ("✏️ Edit", f"fedit:{batch}"), ("✖ Cancel", f"fdrop:{batch}")]]


class Phone:
    def __init__(self, brain: Brain, calendar=lambda: None, help_text=lambda: "") -> None:
        self.brain, self.db, self.settings = brain, brain.db, brain.settings
        self.calendar = calendar  # () -> the Doraemon calendar, or None until Google Calendar is connected
        self.tz = ZoneInfo(self.settings.timezone)
        self.help_text = help_text  # () -> the bot's command list, for messages it doesn't understand

    def answer(self, text: str, now: datetime | None = None, sent: datetime | None = None) -> tuple[str, Buttons | None]:
        """Your message -> (reply, buttons). `sent`: when you sent it, if that was a while ago. Never raises: a
        failure is said, not lost."""
        try:
            now = now or datetime.now(self.tz)
            if sent is not None and now - sent > LATE:
                return self.late(text, sent.astimezone(self.tz))
            return self._answer(text, now)
        except Exception:
            log.exception("answering a Telegram message failed")
            return "Sorry, something went wrong answering that. Try again, or ask in the Doraemon app.", None

    def late(self, text: str, sent: datetime) -> tuple[str, Buttons | None]:
        """A message sent while the bot was off (the laptop asleep): food is still logged, on the day you sent it;
        anything else is said to be left, not silently dropped (a reminder's "in 2 hours" has passed)."""
        day = sent.date()
        at = short_clock(f"{sent:%H:%M}") + ("" if day == self.brain.today() else f" on {sent:%a} {sent.day} {sent:%b}")
        if (repeated := self.repeat(text, day)) is not None:
            return repeated
        sure = coach.food_said(text)
        asks = split_request(text) is not None or split_event(text) is not None  # a reminder or event: left
        if sure or coach.might_be_food(text) and not asks:
            reply, buttons = self.food(sure or text, text, coach.food_day(text, day) or day, sent_at=at)
            if buttons or sure or reply.startswith(MODEL_DOWN):
                return reply, buttons
        return f"I was offline when you sent “{text.strip()[:80]}” ({at}), so I've left it. Send it again if you " \
               "still need it.", None

    def _answer(self, text: str, now: datetime) -> tuple[str, Buttons | None]:
        if (edited := self.edit_food(text, now)) is not None:  # "protein 45, kcal 160" after Edit
            return edited
        asking = self.asked_about(now)
        self.db.set_setting(ASK_KEY, None)
        meal_day = date.fromisoformat(asking["day"]) if asking and asking.get("kind") == "meal" else None
        if (repeated := self.repeat(text, meal_day or self.brain.today())) is not None:  # "same as yesterday"
            return repeated
        if meal_day and not coach.QUESTION.match(text):  # "laksa" after "Lunch at X?"
            return self.food(text, text, meal_day)
        if (asked := split_request(text)) is not None:
            return self.draft(*asked, text, now)
        if (event := split_event(text)) is not None:
            return self.event(event.title, event.when, event.end, text, now, event.kind)
        if target := coach.parse_target(text, self.brain.fit.targets()):  # "change my daily calories to 1950"
            self.brain.fit.set_targets(**target)
            return f"✅ Done: {coach.target_text(target)}.\n\n" + self.plain(coach.targets_card(self.brain.fit)), None
        if card := self.brain.label_amount(text):  # "2 servings", just after a label photo
            return self.food_card(card, "📷 From the label:", label=True)
        if subject := coach.nutrition_subject(text):  # "how many calories in a bowl of greek yogurt?"
            card = self.brain.nutrition_reply(subject)
            if card["kind"] != "food":
                return card["text"], None
            batch = card["payload"]["batch"]
            return card["text"], [[("✅ Log it", f"flog:{batch}"), ("✏️ Edit", f"fedit:{batch}"),
                                   ("✖ No thanks", f"fdrop:{batch}")]]
        if coach.FOOD_IDEAS.search(text):  # "what should I eat for dinner?": the app's own ideas (no model here)
            return self.plain(coach.ideas_card(self.brain.fit)), None
        # "ate chicken rice", "lunch: laksa", "2 eggs, 12g protein" (not a question about your protein), "a protein shake"
        if food := coach.food_said(text):
            return self.food(food, text)
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
        if coach.might_be_food(text):  # "3 eggs and 2 bread", "chicken rice": the model says if it's food
            try:
                reply, buttons = self.food(text, text)
                if buttons or reply.startswith(MODEL_DOWN):  # not "help": it may well be food, and it'll work later
                    return reply, buttons
            except Exception:  # only a guess: what works here instead
                log.exception("asking the model if a Telegram message was food failed")
        log.info("Telegram message not understood: %r", text[:200])
        return self.help_text(), None

    def food(self, food: str, said: str, day: date | None = None, sent_at: str = "") -> tuple[str, Buttons | None]:
        """What you ate, worked out (the model, your foods, Open Food Facts) as a draft with Log and Cancel. "Yesterday"
        in what you `said` puts it on yesterday; `day`, on the day of the meal Dorae-3 asked about. `sent_at`: when a
        message the bot was off for was sent ("1:05pm")."""
        day = day or coach.food_day(said, self.brain.today())
        when = "" if day in (None, self.brain.today()) else " for yesterday" if day == self.brain.today() - timedelta(days=1) \
            else f" for {day:%a} {day.day} {day:%b}"
        intro = f"🍽 From your message at {sent_at}{when}:" if sent_at else f"🍽 Here's what I worked out{when}:"
        return self.food_card(self.brain.food_reply(food, day), intro)

    def repeat(self, text: str, day: date) -> tuple[str, Buttons | None] | None:
        """"Same as yesterday", "my usual breakfast": that meal again as a draft on `day`, or which of yesterday's to
        copy when there were several. None if `text` doesn't ask for a meal again."""
        if (found := nudges.repeat_choices(self.brain.fit, text, day)) is None:
            return None
        which, choices, sure = found
        if not choices:
            return ("Nothing was logged yesterday to copy. Tell me what you had, like “chicken rice and kopi”.", None)
        if sure:
            return self.copy_food(choices[0], day, "🍽 Same as yesterday:" if which == "yesterday" else
                                  f"🍽 Your {which}:" if which.startswith("usual") else f"🍽 Same {which}:")
        lines = [(f"I don't know your {which} yet. " if which.startswith("usual") else "") +
                 "Which one again? Yesterday you logged:"]
        buttons = []
        for rows in choices[:5]:
            names = " + ".join(r["name"] for r in rows)
            lines.append(f"• {names}: {sum(r['kcal'] for r in rows):,.0f} kcal")
            buttons.append([(names if len(names) <= 40 else names[:39] + "…", f"fcopy:{rows[0]['batch']}:{day}")])
        buttons.append(([("All of it", f"fcopy:all:{day}")] if len(choices) > 1 else []) + [("✖ None", "fskip:0")])
        return "\n".join(lines), buttons

    def copy_food(self, rows, day: date, intro: str) -> tuple[str, Buttons | None]:
        batch = self.brain.fit.draft(nudges.copy(rows), day)
        return self.food_card({"kind": "food", "text": "", "payload": {"batch": batch}}, intro)

    def copy_pressed(self, which: str, day: str) -> tuple[str, Buttons | None] | str:
        """A meal pressed under "Which one again?": that batch (or all of yesterday's food) as a draft on `day`."""
        on = date.fromisoformat(day)
        rows = self.brain.fit.foods_on(on - timedelta(days=1))
        rows = rows if which == "all" else [r for r in rows if r["batch"] == which]
        if not rows:
            return "That meal isn't in your log any more."
        return self.copy_food(rows, on, "🍽 Same as yesterday:")

    def something_else(self, batch: str) -> str:
        """Something else under "Chicken rice again?": that draft goes, and your next message is what you had."""
        rows = self.brain.fit.batch(batch)
        if not rows or rows[0]["status"] != "draft":
            return "That one's already logged (or was cancelled)."
        self.brain.fit.cancel(batch)
        self.db.set_setting(ASK_KEY, json.dumps({"kind": "meal", "title": "", "day": rows[0]["day"], "minutes": 180,
                                                 "at": datetime.now(self.tz).isoformat()}))
        return "✏️ What did you have? Reply like “chicken rice and kopi” and I'll work it out."

    def photo(self, image: bytes, caption: str = "") -> tuple[str, Buttons | None]:
        """A photo of a nutrition label or of your food (its caption, e.g. "2 servings", as a hint), as a draft; or a
        screenshot of a booking, for Dorae-2: said in the caption ("hotel"), or found when the photo isn't food."""
        try:
            if BOOKING_HINT.search(caption):
                return self.booking(image, caption)
            card = self.brain.photo_reply(image, caption)
            if card.get("neither"):
                return self.booking(image, caption)
        except Exception:
            log.exception("reading a Telegram photo failed")
            return "Sorry, I couldn't read that photo. Try again, or tell me what you had.", None
        label = card["kind"] == "food" and self.brain.fit.batch(card["payload"]["batch"])[0]["source"] == "label"
        return self.food_card(card, "📷 From the label:" if label else "📷 From your photo (rough numbers):", label)

    def booking(self, image: bytes, caption: str) -> tuple[str, Buttons | None]:
        """The bookings in a screenshot as drafts, each with Add and ✖ (and Add all for several). They show in Dorae-2's
        chat too, where Confirm works the same."""
        card = self.brain.booking_reply(image, caption)
        if card["kind"] != "items":
            return card["text"], None
        self.db.add_message("calendar", "agent", "📷 From a screenshot on Telegram. " + card["text"], card["kind"],
                            card["payload"])
        waiting = [r for r in (self.db.get("item", i) for i in card["payload"]["ids"]) if r["status"] == "proposed"]
        text = card["text"].replace("Check the dates, then press Confirm to add them to your calendar.",
                                    "Check the dates, then press Add to put them in your calendar.")
        buttons = [[(f"✅ Add {r['title'][:28]}", f"make:{r['id']}"), ("✖", f"drop:{r['id']}")] for r in waiting[:6]]
        if len(waiting) > 1:
            buttons.append([("✅ Add all", "makeall:" + ",".join(str(r["id"]) for r in waiting[:6]))])
        return "📷 " + text, buttons or None

    def food_card(self, card: dict, intro: str, label: bool = False) -> tuple[str, Buttons | None]:
        """A food draft as lines with Log and Cancel; anything else as its text."""
        if card["kind"] != "food":
            return card["text"], None
        batch = card["payload"]["batch"]
        hint = ("Had a different amount? Reply like “330 ml”, “2 servings” or “half the pack”." if label else
                "Numbers off? Press Edit: I'll remember what you change.")
        lines = [intro, *coach.food_lines(self.brain.fit.batch(batch)), "", hint]
        return "\n".join(lines), food_buttons(batch)

    def live_food(self, batch: str) -> list:
        return [r for r in self.brain.fit.batch(batch) if r["status"] != "cancelled"]

    def start_edit(self, batch: str) -> tuple[str, Buttons | None]:
        """Edit pressed under food: the next message ("protein 45, kcal 160") changes its numbers."""
        rows = self.live_food(batch)
        if not rows:
            return "That one was cancelled.", None
        self.db.set_setting(EDIT_KEY, json.dumps({"batch": batch, "at": datetime.now(self.tz).isoformat()}))
        which = f", starting with the food, like “{rows[-1]['name']}: kcal 80”" if len(rows) > 1 else ""
        text = f"✏️ Send the new numbers, like “protein 45, kcal 160”{which}."
        drafts = any(r["status"] == "draft" for r in rows)
        return text, food_buttons(batch) if drafts else [[("✏️ Edit", f"fedit:{batch}")]]

    def edit_food(self, text: str, now: datetime) -> tuple[str, Buttons | None] | None:
        """A message with new numbers for the food Edit was pressed under; None if it isn't one (or Edit is stale)."""
        try:
            editing = json.loads(self.db.get_setting(EDIT_KEY) or "null")
            fresh = now - datetime.fromisoformat(editing["at"]) < timedelta(minutes=ASK_MINUTES)
        except (ValueError, TypeError, KeyError):
            return None
        new_food = NEW_FOOD.match(text) or coach.food_request(text, own_chat=False)  # "had 2 eggs, 12g protein"
        changes = edit_numbers(text) if not new_food else {}
        if not fresh or not changes:
            self.db.set_setting(EDIT_KEY, None)  # something else: Edit is over
            return None
        batch = editing["batch"]
        rows = self.live_food(batch)
        if not rows:
            self.db.set_setting(EDIT_KEY, None)
            return "That one was cancelled, so there's nothing to change.", None
        words = set(key_of(text).split())
        scored = sorted(((len(words & {w for w in key_of(r["name"]).split() if len(w) > 2}), r) for r in rows),
                        key=lambda s: -s[0])
        if len(rows) > 1 and (scored[0][0] == 0 or scored[0][0] == scored[1][0]):
            return (f"Which one? Start with the food, like “{rows[-1]['name']}: "
                    + ", ".join(f"{k} {v:g}" for k, v in changes.items()) + "”."), None
        row = rows[0] if len(rows) == 1 else scored[0][1]
        self.brain.fit.edit(row["id"], **changes)  # a logged one: the library learns it too
        self.db.set_setting(EDIT_KEY, None)
        rows = self.live_food(batch)
        logged = all(r["status"] == "logged" for r in rows)
        lines = ["✏️ Updated:", *coach.food_lines(rows), "", self.today_text() if logged else
                 "I'll remember these numbers. Press Log when it's right."]
        return "\n".join(lines), [[("✏️ Edit", f"fedit:{batch}")]] if logged else food_buttons(batch)

    def today_text(self) -> str:
        s = self.brain.fit.day_status()
        t, goals = s["totals"], s["targets"]
        return (f"Today: {t.kcal:,.0f}" + (f" of {goals['kcal']:,}" if goals["kcal"] else "") + " kcal, "
                f"{t.protein:.0f}" + (f" of {goals['protein']}" if goals["protein"] else "") + " g protein.")

    def log_food(self, batch: str) -> str:
        rows = self.brain.fit.log(batch)
        if not rows:
            return "That one's already logged (or was cancelled)."
        self.db.add_message("fitness", "agent", f"🍽 Logged on Telegram: " + ", ".join(r["name"] for r in rows)
                            + f" ({sum(r['kcal'] for r in rows):,.0f} kcal).")
        return f"✅ Logged. {self.today_text()}"

    def asked_about(self, now: datetime) -> dict | None:
        """{"title", "kind"} of the reminder or event the bot just asked "when?" about (or the meal Dorae-3 asked
        "what did you have?" about), if it's still a fresh question."""
        try:
            asked = json.loads(self.db.get_setting(ASK_KEY) or "null")
            at = datetime.fromisoformat(asked["at"])
        except (ValueError, TypeError, KeyError):
            return None
        return asked if now - at < timedelta(minutes=asked.get("minutes", ASK_MINUTES)) else None

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

    def event(self, title: str, when: str, end: str, question: str, now: datetime,
              kind: str = "") -> tuple[str, Buttons | None]:
        result, item_id = draft_event(self.db, self.settings.timezone, self.settings.date_order, title, when,
                                      question, now, end=end, kind=kind)
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
        elif kind == "meal_ideas":
            lines = ([f"• {' + '.join(pl['foods'])}: {pl['kcal']:,} kcal, {pl['protein']} g protein" for pl in p["plans"][1:]]
                     if p.get("plans") else [f"• {o[0]}: {o[1]} kcal, {o[2]} g protein" for o in p["options"][3:6]])
            if lines:
                lines.insert(0, "Or:")
        elif kind == "fitness_review":
            lines = [f"• {r[0]}: " + (f"{r[1]} of {r[2]} a day, {r[4]}" if r[1] != "–" else r[4]) for r in p["rows"]]
            if p["foods"]:
                lines.append("Most calories from: " + ", ".join(f"{f[0]} ({f[1]}×)" for f in p["foods"]))
            if p["light"]:
                lines.append("Left out as partly logged: " + ", ".join(d[0] for d in p["light"]))
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

