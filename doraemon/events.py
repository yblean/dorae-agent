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

_POLITE = r"(?:(?:hey|hi|ok|okay|pls|please|can you|could you|can u|pls can you|help me(?: to)?)\W+)*"
# A name you give it: 'titled "Zx Convocation"', 'called Sam's farewell', or just in quotes
_NAMED = re.compile(r"""\b(?:titled|called|named)\s+["“'](?P<q>[^"”']+)["”']|["“](?P<q2>[^"”]+)["”]|"""
                    r"""\b(?:titled|called|named)\s+(?P<w>.+?)(?=\s+(?:on|at|for|this|next|tomorrow|today)\b|$)""", re.I)
_CLOCK_WORD = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\b\.?|\b\d{1,2}:\d{2}\b", re.I)
_THING = r"(?:(?:new\s+)?(?:calendar\s+)?(?:event|appointment|appt|meeting))"
# "add ...", "schedule ...", "make an event ...", "new event: ...", "event: ...". "event" alone, or any of them
# before for/called/:, is left out of the title; "meeting with prof" keeps its "meeting".
_ASK_RE = re.compile(
    rf"^\s*{_POLITE}(?:(?P<verb>add|put|schedule|plan|create|set\s+up|set|make|new|log|book|block(?:\s+out)?|note(?:\s+down)?|"
    rf"save)\s+(?:me\s+)?(?:an?\s+|the\s+|my\s+)?"
    rf"(?:{_THING}\s*(?:for|called|named|:)\s*|(?:new\s+)?(?:calendar\s+)?event\s+)?|(?:{_THING}|calendar)\s*:\s*)"
    rf"(?P<rest>.+)$", re.I | re.S)
_SAYS_EVENT = re.compile(r"\b(?:event|appointment|appt|meeting|calendar)\b", re.I)
_CALENDAR = re.compile(r"\s*\b(?:to|on|in|into)\s+(?:my|the)\s+(?:google\s+)?(?:calendar|cal|schedule|agenda)\b", re.I)
_CLOCK = r"\d{1,2}(?::\d{2})?"
_AMPM = r"[ap]\.?m\b\.?"
_DAY = r"\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?"
_DAYS_RE = re.compile(rf"\b({_DAY})\s*(?:-|–|\bto\b|\btill\b|\buntil\b)\s*({_DAY})\b", re.I)  # 26/10-28/10
# Days with a month's name: "5th Nov to 7th Nov", "5-7 Nov", "5th to 7th november", "Nov 5 - Nov 7", "nov 5-7"
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_ORD = r"\d{1,2}(?:st|nd|rd|th)?"
_THRU = r"\s*(?:-|–|\bto\b|\btill\b|\buntil\b|\bthrough\b|\bthru\b)\s*"
_YEAR = r"(?:,?\s+(?P<year>\d{4}))?"
_NOT_CLOCK = r"(?!\s*(?:[ap]\.?m\b|:\d))"
_NAMED_DAYS = [(re.compile(rf"(?:\bfrom\s+)?\b{p}{_YEAR}\b", re.I), month_first) for p, month_first in (
    (rf"(?P<d1>{_ORD})\s+(?:of\s+)?(?P<m1>{_MONTH}){_THRU}(?P<d2>{_ORD})\s+(?:of\s+)?(?P<m2>{_MONTH})", False),
    (rf"(?P<d1>{_ORD}){_THRU}(?P<d2>{_ORD})\s+(?:of\s+)?(?P<m2>{_MONTH})", False),
    (rf"(?P<m1>{_MONTH})\s+(?P<d1>{_ORD}){_THRU}(?P<m2>{_MONTH})\s+(?P<d2>{_ORD}){_NOT_CLOCK}", True),
    (rf"(?P<m1>{_MONTH})\s+(?P<d1>{_ORD}){_THRU}(?P<d2>{_ORD}){_NOT_CLOCK}", True))]
# What kind of thing it is, from its words: "Dali Cang'er Hotel", "flight to Tokyo", "trip to Bali". "as travel" or
# "as a hotel" says it outright, and leaves the title.
_KINDS = [(ItemType.HOTEL, r"hotels?|hostel|resort|airbnb|inn|motel|stay|check[- ]?in|accommodation"),
          (ItemType.FLIGHT, r"flights?|fly|flying"),
          (ItemType.OTHER_TRAVEL, r"trips?|travel|travelling|traveling|holiday|vacation|getaway|overseas"),
          (ItemType.APPOINTMENT, r"appointment|event|meeting")]
_AS_KIND = re.compile(r"\s+as\s+(?:an?\s+)?(?P<kind>" + "|".join(w for _, w in _KINDS) + r")\b", re.I)
_LOG_VERBS = ("log", "book", "block", "block out", "note", "note down", "save")


def kind_of(text: str) -> ItemType | None:
    """The type a title's words say (a hotel, a flight, other travel), or None for an ordinary event."""
    for kind, words in _KINDS[:3]:
        if re.search(rf"\b(?:{words})\b", text, re.I):
            return kind
    return None


def _named_days(text: str) -> tuple[str, str, str] | None:
    """(text with the range replaced by its first day, first day, last day) for "5th Nov to 7th Nov", or None."""
    for pattern, month_first in _NAMED_DAYS:
        if m := pattern.search(text):
            found = m.groupdict()
            year = f" {found['year']}" if found["year"] else ""
            m1 = found.get("m1") or found["m2"]
            m2 = found.get("m2") or m1
            first, last = ((f"{m1} {found['d1']}", f"{m2} {found['d2']}") if month_first else
                           (f"{found['d1']} {m1}", f"{found['d2']} {m2}"))
            return text[:m.start()] + f" {first}{year} " + text[m.end():], first + year, last + year
    return None
# "7-9pm", "7pm to 9pm", "from 19:00 until 21:00"
_RANGE_RE = re.compile(rf"(?:\bfrom\s+)?\b({_CLOCK})\s*({_AMPM})?\s*(?:-|–|\bto\b|\btill\b|\buntil\b)\s*({_CLOCK})\s*({_AMPM})?",
                       re.I)


@dataclass
class EventAsk:
    title: str
    when: str      # the date and start time words, "" if none were given
    end: str = ""  # an end time ("9pm") or last day ("28/10") you gave, or ""
    kind: str = ""  # "hotel", "flight" or "other_travel" when its words say so; "" for an appointment


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
    if (asked["verb"] or "").lower() in ("make", "new", "set") and not says_event:
        return None  # "make dinner tomorrow" is a plan, "set my budget" a budget: not requests for an event
    rest = _CALENDAR.sub(" ", asked["rest"])
    kind = None
    if said := _AS_KIND.search(rest):  # "as travel": the type you want, not part of the name
        kind = kind_of(said["kind"]) or ItemType.APPOINTMENT
        rest = rest[:said.start()] + " " + rest[said.end():]
    named = _NAMED.search(rest)
    if named:  # the title you gave; the date and time come from the rest
        rest = rest[:named.start()] + " " + rest[named.end():]
    end = ""
    if days := _DAYS_RE.search(rest):
        rest, end = rest[:days.start()] + f" {days[1]} " + rest[days.end():], days[2]
    elif days := _named_days(rest):
        rest, _, end = days
    elif ranged := _time_range(rest):
        rest, _, end = ranged
    verb = re.sub(r"\s+", " ", (asked["verb"] or "").lower())
    if verb in _LOG_VERBS and not (days or kind or kind_of(rest) or says_event):
        return None  # "log laksa yesterday" is food; "log my hotel 5-7 nov" is for the calendar
    title, when = split_when(rest)
    if named:
        name = (named["q"] or named["q2"] or named["w"]).strip(" .,")
        title = (name[:1].upper() + name[1:])[:80]
    clocks = _CLOCK_WORD.findall(when)
    if len(clocks) > 1 and len({c.replace(" ", "").lower() for c in clocks}) == 1:  # "friday 2pm ... at 2pm"
        first = when.find(clocks[0]) + len(clocks[0])
        when = when[:first] + re.sub(rf"\s*(?:\bat\s+)?{re.escape(clocks[0])}", "", when[first:])
    if not when and not says_event:
        return None
    if re.search(r"\b(?:how|what|why|which)\b|\?", title, re.I):  # "add X on 26/10 and how much did I spend": a question too
        return None
    kind = kind or kind_of(title)
    return EventAsk(title, when, end, kind.value if kind and kind != ItemType.APPOINTMENT else "")


def draft_event(db: Database, user_tz: str, date_order: str, title: str, when: str, question: str,
                now: datetime, end: str = "", location: str | None = None, kind: str = "") -> tuple[dict, int | None]:
    """Save the event as a card waiting for your OK (an appointment, or the `kind` its words said: a hotel...).
    Returns (what to say, item id): {"error"}, {"already_there", "title", "when"} or {"drafted", "title", "when"};
    the id is None after an error."""
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
    item = ActionItem(message_id="chat", type=ItemType(kind or ItemType.APPOINTMENT), title=title, start_at=start, end_at=finish,
                      all_day=not has_time, timezone=user_tz, location=location, date_text=when,
                      evidence_snippet=question[:200], confidence=1.0)
    item_id = db.add_item(f"{CHAT_ID_PREFIX}{uuid.uuid4().hex[:12]}", item)
    return {"drafted": True, "title": title, "when": describe_when(item, user_tz)}, item_id
