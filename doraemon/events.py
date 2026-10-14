"""Calendar events you ask for in your own words: "add date night on 26/10 7-9pm".

Read by fixed rules, so the Telegram bot can draft one without the model: the title is what's left
once the date and time words are taken out. The draft waits for your OK; only then does it go to
Google Calendar (doraemon.calendar_sync.add_to_calendar).

    split_event("add dinner with Sam tmr 7-9pm to my calendar")
    -> EventAsk(title="Dinner with Sam", when="tomorrow 7pm", end="9pm")
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from doraemon.dates import resolve_spoken, unslang
from doraemon.db import CHAT_ID_PREFIX, Database, item_model
from doraemon.display import describe_when
from doraemon.reminders import WANTS_REMINDER, split_when
from doraemon.schema import ActionItem, ItemType

_POLITE = r"(?:(?:hey|hi|ok|okay|pls|please|can you|could you|can u|pls can you)\W+)*"
_THING = r"(?:(?:new\s+)?(?:calendar\s+)?(?:event|appointment|appt|meeting))"
# "add ...", "schedule ...", "make an event ...", "new event: ...", "event: ...". "event" alone, or any of them
# before for/called/:, is left out of the title; "meeting with prof" keeps its "meeting".
_ASK_RE = re.compile(
    rf"^\s*{_POLITE}(?:(?P<verb>add|put|schedule|plan|create|set\s+up|make|new)\s+(?:me\s+)?(?:an?\s+|the\s+)?"
    rf"(?:{_THING}\s*(?:for|called|named|:)\s*|(?:new\s+)?(?:calendar\s+)?event\s+)?|(?:{_THING}|calendar)\s*:\s*)"
    rf"(?P<rest>.+)$", re.I | re.S)
_SAYS_EVENT = re.compile(r"\b(?:event|appointment|appt|meeting|calendar)\b", re.I)
_CALENDAR = re.compile(r"\s*\b(?:to|on|in|into)\s+(?:my|the)\s+(?:google\s+)?(?:calendar|cal|schedule|agenda)\b", re.I)
_CLOCK = r"\d{1,2}(?::\d{2})?"
_AMPM = r"[ap]\.?m\b\.?"
_DAY = r"\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?"
_DAYS_RE = re.compile(rf"\b({_DAY})\s*(?:-|–|\bto\b|\btill\b|\buntil\b)\s*({_DAY})\b", re.I)  # 26/10-28/10
# "7-9pm", "7pm to 9pm", "from 19:00 until 21:00"
_RANGE_RE = re.compile(rf"(?:\bfrom\s+)?\b({_CLOCK})\s*({_AMPM})?\s*(?:-|–|\bto\b|\btill\b|\buntil\b)\s*({_CLOCK})\s*({_AMPM})?",
                       re.I)


@dataclass
class EventAsk:
    title: str
    when: str      # the date and start time words, "" if none were given
    end: str = ""  # an end time ("9pm") or last day ("28/10") you gave, or ""


def _time_range(text: str) -> tuple[str, str, str] | None:
    """(text with the range replaced by its start, start, end) for "7-9pm"; None if there's no time range.
    A range needs a clock on at least one side (am/pm or hh:mm), so dates like 26/10-27/10 aren't read as times."""
    for m in _RANGE_RE.finditer(text):
        start, start_ap, end, end_ap = m[1], m[2] or "", m[3], m[4] or ""
        if not (start_ap or end_ap or ":" in start + end):
            continue
        if text[m.start() - 1:m.start()] in "/.-" and m.start() > 0:
            continue  # part of a date
        if not start_ap and end_ap and ":" not in start:  # "7-9pm" is 7pm; "11-1pm" is 11am
            start_ap = end_ap if int(start) <= int(end.split(":")[0]) or int(start) == 12 else \
                ("am" if end_ap.lower().startswith("p") else "pm")
        start_text, end_text = f"{start}{start_ap}", f"{end}{end_ap}"
        return text[:m.start()] + f" {start_text} " + text[m.end():], start_text, end_text
    return None


def split_event(text: str) -> EventAsk | None:
    """An event request, or None if the message isn't one. Needs a date, or the word event / appointment /
    meeting / calendar, so "add 300 to my dining budget" isn't taken for an event."""
    text = unslang(text)
    asked = _ASK_RE.match(text)
    if asked is None or WANTS_REMINDER.search(text):  # reminders are doraemon.reminders' to read
        return None
    says_event = bool(_SAYS_EVENT.search(text))
    if (asked["verb"] or "").lower() in ("make", "new") and not says_event:
        return None  # "make dinner tomorrow" is a plan, not a request for an event
    rest = _CALENDAR.sub(" ", asked["rest"])
    end = ""
    if days := _DAYS_RE.search(rest):
        rest, end = rest[:days.start()] + f" {days[1]} " + rest[days.end():], days[2]
    elif ranged := _time_range(rest):
        rest, _, end = ranged
    title, when = split_when(rest)
    if not when and not says_event:
        return None
    return EventAsk(title, when, end)


def draft_event(db: Database, user_tz: str, date_order: str, title: str, when: str, question: str,
                now: datetime, end: str = "", location: str | None = None) -> tuple[dict, int | None]:
    """Save the event as a card waiting for your OK. Returns (what to say, item id): {"error"}, {"already_there",
    "title", "when"} or {"drafted", "title", "when"}; the id is None after an error."""
    if not title:
        return {"error": "What's the event called? For example: add date night on 26/10 7-9pm."}, None
    start, has_time = resolve_spoken(when, now, user_tz, date_order)
    if start is None:
        return {"error": (f"I couldn't read a date from “{when}”. " if when else "")
                + f"When is “{title}”? For example: tomorrow 3pm, or 26/10 7-9pm."}, None
    if start.date() < now.astimezone(ZoneInfo(user_tz)).date():
        return {"error": f"“{when}” reads as {start:%a} {start.day} {start:%b %Y}, which has passed. When is it?"}, None
    finish = None
    if end:  # a bare end time is that evening, or past midnight; a last day is that day
        finish, end_has_time = resolve_spoken(end, start - timedelta(minutes=1), user_tz, date_order)
        if finish is None or finish <= start or end_has_time != has_time:
            finish = None
    for r in db.items(("proposed", "confirmed")):  # asked twice: show the one already there
        if r["title"].lower() == title.lower() and r["start_at"] == start.isoformat():
            return {"already_there": True, "title": r["title"], "when": describe_when(item_model(r), user_tz)}, r["id"]
    item = ActionItem(message_id="chat", type=ItemType.APPOINTMENT, title=title, start_at=start, end_at=finish,
                      all_day=not has_time, timezone=user_tz, location=location, date_text=when,
                      evidence_snippet=question[:200], confidence=1.0)
    item_id = db.add_item(f"{CHAT_ID_PREFIX}{uuid.uuid4().hex[:12]}", item)
    return {"drafted": True, "title": title, "when": describe_when(item, user_tz)}, item_id
