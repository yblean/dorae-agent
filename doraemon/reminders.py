"""Reminders on Telegram for bills, deadlines, deliveries and RSVPs, instead of calendar events.

You pick when the first reminder comes (a week, 3 days, a day, 3 hours, an hour before, or at
the time) and how many you want; later ones step down the same ladder, so "1 day before, 3 times"
means 1 day, 3 hours and 1 hour before. All-day items (a bill due on the 15th) count from 9am
that day and only use whole days: the day before, then that morning.

While the web app is open it sends what's due every minute. If it was closed when a reminder
was due, it sends the latest missed one when it starts again, as long as the item hasn't passed.
"""
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from doraemon.calendar_sync import ALL_DAY_TYPES, REMINDER_TYPES, gmail_link
from doraemon.dates import resolve_spoken, unslang
from doraemon.db import CHAT_ID_PREFIX, Database, from_chat, item_model
from doraemon.display import describe_when
from doraemon.schema import ActionItem, ItemType

log = logging.getLogger(__name__)

MORNING = time(9)  # all-day items count back from 9am on the day


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    minutes: int


TIMED_STEPS = [Step("1w", "1 week before", 7 * 1440), Step("3d", "3 days before", 3 * 1440),
               Step("1d", "1 day before", 1440), Step("3h", "3 hours before", 180),
               Step("1h", "1 hour before", 60), Step("0", "at the time", 0)]
ALL_DAY_STEPS = [Step("1w", "1 week before", 7 * 1440), Step("3d", "3 days before", 3 * 1440),
                 Step("1d", "the day before", 1440), Step("0", "that morning", 0)]
DEFAULT_FIRST, DEFAULT_TIMES, MAX_TIMES = "1d", 2, 4
VERB = {"bill": "Due", "deadline": "Due", "delivery": "Arriving", "rsvp": "Reply by", "reminder": ""}


def defaults_for(item_type: str) -> tuple[str, int]:
    """(first, times) a card starts with. "Remind me to X tomorrow" means once, at the time
    (or 9am for a whole day); bills and deadlines get a heads-up the day before as well."""
    return ("0", 1) if item_type == ItemType.REMINDER else (DEFAULT_FIRST, DEFAULT_TIMES)


def wants_reminder(item_type: str) -> bool:
    return item_type in {t.value for t in REMINDER_TYPES}


def counts_as_all_day(item: ActionItem) -> bool:
    return item.all_day or item.type in ALL_DAY_TYPES


def steps_for(item: ActionItem) -> list[Step]:
    return ALL_DAY_STEPS if counts_as_all_day(item) else TIMED_STEPS


def due_at(item: ActionItem, user_tz: str) -> datetime | None:
    """The moment reminders count back from: the item's time, or 9am on the day for all-day items."""
    if item.start_at is None:
        return None
    if counts_as_all_day(item):
        day = item.start_at.astimezone(ZoneInfo(item.timezone)).date()
        return datetime.combine(day, MORNING, ZoneInfo(user_tz))
    return item.start_at


def too_late(item: ActionItem, user_tz: str, now: datetime) -> bool:
    """True once the item is over: an hour after a timed one, or the end of the day for an all-day one."""
    due = due_at(item, user_tz)
    if due is None:
        return True
    if counts_as_all_day(item):
        return now >= datetime.combine(due.date() + timedelta(days=1), time(0), due.tzinfo)
    return now >= due + timedelta(hours=1)


def plan(item: ActionItem, user_tz: str, first: str, times: int, now: datetime) -> tuple[list[dict], list[str]]:
    """Reminder times for `times` reminders starting at `first` and stepping down the ladder.

    Returns (planned [{"at", "label"}], labels of the ones dropped because that time has passed).
    """
    due = due_at(item, user_tz)
    if due is None:
        return [], []
    steps = steps_for(item)
    start = next((i for i, s in enumerate(steps) if s.key == first), None)
    if start is None:  # e.g. "3 hours before" on an all-day item: the nearest whole day instead
        minutes = next((s.minutes for s in TIMED_STEPS if s.key == first), 1440)
        start = next((i for i, s in enumerate(steps) if s.minutes <= minutes), len(steps) - 1)
    chosen = steps[start:start + max(1, min(times, MAX_TIMES))]
    planned, passed = [], []
    for s in chosen:
        at = due - timedelta(minutes=s.minutes)
        if at > now:
            planned.append({"at": at, "label": s.label})
        else:
            passed.append(s.label)
    return planned, passed


def short_time(dt: datetime, tz: ZoneInfo) -> str:
    """'Thu 14 Oct, 9am' in your timezone."""
    local = dt.astimezone(tz)
    clock = f"{local.hour % 12 or 12}{f':{local.minute:02d}' if local.minute else ''}{'am' if local.hour < 12 else 'pm'}"
    return f"{local:%a} {local.day} {local:%b}, {clock}"


def when_line(item: ActionItem, user_tz: str, now: datetime) -> str:
    """'Due tomorrow, Thu 15 Oct' or 'Due today, Thu 15 Oct at 2pm (in 3 hours)'."""
    tz = ZoneInfo(user_tz)
    due = due_at(item, user_tz).astimezone(tz)
    days = (due.date() - now.astimezone(tz).date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days > 1 else "earlier"
    verb = VERB.get(item.type.value, "On")
    when = f"{verb} {day}, {due:%a} {due.day} {due:%b}" if verb else f"{day.capitalize()}, {due:%a} {due.day} {due:%b}"
    if not counts_as_all_day(item):
        when += f" at {short_time(due, tz).split(', ')[1]}"
        hours = (due - now).total_seconds() / 3600
        if 0 < hours < 24:
            when += f" (in {round(hours)} hour{'s' if round(hours) != 1 else ''})" if hours >= 1 else " (very soon)"
    return when


# --- reminders you ask Dorae-2 for in chat ------------------------------------------------

_ASK_RE = re.compile(r"\b(?:remind\s+me|set\s+(?:me\s+)?(?:a\s+)?reminder|(?:create|make|add)\s+(?:me\s+)?(?:a\s+)?"
                     r"reminder|reminder|(?:ping|nudge|alert)\s+me|(?:don'?t|do\s+not)\s+let\s+me\s+forget)\b[:,]?"
                     r"(?:\s+(?:to|for|about|that))?\s+(.+)", re.I | re.S)
# A message asking to be reminded, however it's worded: such requests become reminders, never calendar events
WANTS_REMINDER = re.compile(r"\b(?:remind|reminders?|(?:ping|nudge|alert)\s+me|(?:don'?t|do\s+not)\s+let\s+me\s+forget)\b",
                            re.I)
_WEEKDAY = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday)?"
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*[ap]\.?m\b\.?|\d{1,2}:\d{2}\b|noon\b|midnight\b)"
# Date and time words in a request, so "go to get groceries tomorrow 5pm" splits into title and when
_WHEN_RE = re.compile(
    rf"(?:\b(?:on|at|by|this|next|coming)\s+)*(?:\btoday\b|\btomorrow\b|\b{_WEEKDAY}\b|\bin\s+(?:\d+|an?)\s*"
    rf"(?:mins?|minutes?|hrs?|hours?|days?|weeks?)\b|\b\d{{1,2}}[/.-]\d{{1,2}}(?:[/.-]\d{{2,4}})?\b|"
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\b|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b{_CLOCK}|\bnext\s+week\b)",
    re.I)


def split_request(text: str) -> tuple[str, str] | None:
    """("Go to get groceries", "tomorrow") from "create me a reminder to go to get groceries tmr", or None
    if it isn't a reminder request. `when` is "" if no date was given."""
    asked = _ASK_RE.search(unslang(text))
    if asked is None:
        return None
    rest = re.sub(r"\s*(?:pls|please|thanks|thank you|thx|ty)\W*$", "", asked[1].strip(), flags=re.I)
    spans = list(_WHEN_RE.finditer(rest))
    when = " ".join(m[0].strip() for m in spans)
    title = _WHEN_RE.sub(" ", rest)
    title = re.sub(r"\s+", " ", title).strip(" ,.!?")
    title = re.sub(r"^(?:to|for|about|that)\s+|\s+(?:on|at|by)$", "", title, flags=re.I)
    return (title[:1].upper() + title[1:])[:80], when


def draft_reminder(db: Database, user_tz: str, date_order: str, title: str, when: str, question: str,
                   now: datetime) -> tuple[dict, int | None]:
    """Save the reminder as a card waiting for your OK. Returns (what to tell the model or say, item id)."""
    # Errors are worded for you and the model alike: Dorae-2 shows them as they are when it isn't using the model
    if not title:
        return {"error": "What should I remind you about? For example: remind me to get groceries tomorrow."}, None
    start, has_time = resolve_spoken(when, now, user_tz, date_order)
    if start is None:
        return {"error": (f"I couldn't read a date from “{when}”. " if when else "") +
                f"When should I remind you to {title[:1].lower() + title[1:]}? For example: tomorrow 5pm, or 26/10."}, None
    if (start <= now) if has_time else (start.date() < now.astimezone(ZoneInfo(user_tz)).date()):
        return {"error": f"“{when}” reads as {start:%a} {start.day} {start:%b %Y}" + (f", {start:%H:%M}" if has_time else "")
                + ", which has already passed. When should I remind you?"}, None
    for r in db.items(("proposed", "confirmed")):  # asked twice: show the one already there
        if r["type"] == ItemType.REMINDER and r["title"].lower() == title.lower() and r["start_at"] == start.isoformat():
            return {"already_there": True, "title": r["title"], "when": describe_when(item_model(r), user_tz)}, r["id"]
    item = ActionItem(message_id="chat", type=ItemType.REMINDER, title=title, start_at=start, end_at=None,
                      all_day=not has_time, timezone=user_tz, date_text=when, evidence_snippet=question[:200],
                      confidence=1.0)
    item_id = db.add_item(f"{CHAT_ID_PREFIX}{uuid.uuid4().hex[:12]}", item)
    first, times = defaults_for(ItemType.REMINDER)
    planned, _ = plan(item, user_tz, first, times, now)
    return {"drafted": True, "title": title, "when": describe_when(item, user_tz),
            "reminds": ", ".join(f"{short_time(r['at'], ZoneInfo(user_tz))} ({r['label']})" for r in planned),
            "next_step": "The user can edit the card, then must press Create reminder."}, item_id


def created_text(item: ActionItem, user_tz: str, planned: list[dict], now: datetime, updated: bool = False) -> str:
    """Sent on Telegram right after you set (or change) an item's reminders, listing when they'll come."""
    tz = ZoneInfo(user_tz)
    head = "✏️ Reminders updated" if updated else "✅ Reminder created"
    lines = [f"{head}: {item.title}", when_line(item, user_tz, now), "", "I'll remind you:"]
    lines += [f"• {short_time(r['at'], tz)} ({r['label']})" for r in planned]
    return "\n".join(lines)


def list_text(db: Database, user_tz: str, now: datetime | None = None, limit: int = 15) -> str:
    """Your reminders for the bot's /reminders command: each item with its unsent reminder times."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(user_tz)
    by_item: dict[int, list] = {}
    for r in db.upcoming_reminders():  # soonest first, so items come in the order they'll remind you
        by_item.setdefault(r["item_id"], []).append(r)
    items = [(item, rows) for item_id, rows in by_item.items()
             if not too_late(item := item_model(db.get("item", item_id)), user_tz, now)]
    if not items:
        return ("No reminders set. Ask Dorae-2 (\"remind me to get groceries tomorrow\"), or press "
                "Remind me on a bill, deadline or delivery in Doraemon.")
    head = f"🔔 Your reminders ({len(items)} item{'s' if len(items) != 1 else ''})"
    blocks = []
    for item, rows in items[:limit]:
        lines = [item.title, when_line(item, user_tz, now)
                 + (f" · {item.currency} {item.amount}" if item.amount is not None else "")]
        lines += [f"• {short_time(datetime.fromisoformat(r['remind_at']), tz)} ({r['label']})" for r in rows]
        blocks.append("\n".join(lines))
    if len(items) > limit:
        blocks.append(f"…and {len(items) - limit} more. See them all under Upcoming in Doraemon.")
    return head + "\n\n" + "\n\n".join(blocks)


def message_text(item: ActionItem, gmail_id: str, user_tz: str, now: datetime) -> str:
    """The Telegram message. Plain text (no formatting), since titles come from email."""
    lines = [f"🔔 {item.title}", when_line(item, user_tz, now)]
    if item.amount is not None:
        lines.append(f"Amount: {item.currency} {item.amount}")
    if item.location:
        lines.append(f"Where: {item.location}")
    if item.booking_ref:
        lines.append(f"Ref: {item.booking_ref}")
    if not from_chat(gmail_id):
        lines.append(f"Email: {gmail_link(gmail_id)}")
    return "\n".join(lines)


class Messenger(Protocol):
    def send(self, text: str) -> None: ...


def send_due(db: Database, messenger: Messenger | None, user_tz: str, now: datetime | None = None) -> dict:
    """Send the reminders that are due. Per item, only the latest due one goes out; earlier
    ones it missed (the app was closed) are marked missed rather than sent in a burst.

    Returns counts: sent, missed, cancelled, waiting (no Telegram yet), failed (+ the last error).
    """
    now = now or datetime.now(timezone.utc)
    result = {"sent": 0, "missed": 0, "cancelled": 0, "waiting": 0, "failed": 0, "error": ""}
    by_item: dict[int, list] = {}
    for r in db.due_reminders(now):
        by_item.setdefault(r["item_id"], []).append(r)
    for item_id, due in by_item.items():
        row = db.get("item", item_id)
        if row is None or row["status"] != "confirmed":  # undone or dismissed since
            for r in due:
                db.mark_reminder(r["id"], "cancelled")
            result["cancelled"] += len(due)
            continue
        item = item_model(row)
        *older, latest = due  # sorted by time
        for r in older:
            db.mark_reminder(r["id"], "missed")
        result["missed"] += len(older)
        if too_late(item, user_tz, now):
            db.mark_reminder(latest["id"], "missed")
            result["missed"] += 1
            continue
        if messenger is None:
            result["waiting"] += 1
            continue
        try:
            messenger.send(message_text(item, row["gmail_id"], user_tz, now))
        except Exception as e:  # stays pending; tried again next minute
            result["failed"] += 1
            result["error"] = str(e)[:200]
            continue
        db.mark_reminder(latest["id"], "sent")
        result["sent"] += 1
    return result


class ReminderJob:
    """Sends due reminders every minute while the web app is open, and says problems once in Dorae-2's chat."""

    def __init__(self, db: Database, user_tz: str, messenger) -> None:
        self.db, self.user_tz = db, user_tz
        self.messenger = messenger  # () -> Messenger | None, so connecting Telegram later just works
        self.last_problem = ""

    def tick(self, now: datetime | None = None) -> dict:
        result = send_due(self.db, self.messenger(), self.user_tz, now)
        if result["waiting"]:
            self.problem("A reminder is due, but Telegram isn't connected yet. "
                         "Run: python -m doraemon.telegram connect")
        elif result["failed"]:
            self.problem(f"I couldn't send a reminder on Telegram ({result['error']}). I'll keep trying.")
        elif result["sent"]:
            self.last_problem = ""
        return result

    def problem(self, text: str) -> None:
        if text != self.last_problem:
            self.last_problem = text
            self.db.add_message("calendar", "agent", text)

    def run_every(self, seconds: int = 60) -> threading.Event:
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(seconds):
                try:
                    self.tick()
                except Exception:  # keep the loop alive; the next minute tries again
                    log.exception("sending reminders failed")
        threading.Thread(target=loop, daemon=True, name="reminders").start()
        return stop
