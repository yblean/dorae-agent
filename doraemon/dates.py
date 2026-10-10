"""Deterministic date parsing. No LLM touches dates after extraction."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates

_TIME_RE = re.compile(r"\d{1,2}:\d{2}|\d\s*[ap]\.?m\b|\bnoon\b|\bmidnight\b", re.I)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_YMD_RE = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})(.*)$")


def _parse(text: str, settings: dict) -> datetime | None:
    parsed = dateparser.parse(text, settings=settings)
    if parsed is None:
        found = search_dates(text, settings=settings)
        if found:
            parsed = found[0][1]
    return parsed


def resolve(
    text: str | None, reference: datetime, tz: str, date_order: str = "DMY"
) -> tuple[datetime | None, bool]:
    """Turn date text from an email into an aware datetime in `tz`.

    `reference` is when the email was sent, so "next Friday" means the Friday
    after the email, not after today. Returns (datetime, has_time); date-only
    text comes back at midnight with has_time False, i.e. an all-day item.
    """
    if not text or not text.strip():
        return None, False

    zone = ZoneInfo(tz)

    # Year-first dates ("2026/04/10 (Fri) 00:30", common in Japanese and Chinese emails)
    # are always year-month-day; rewrite them as ISO.
    ymd = _YMD_RE.match(text.strip())
    if ymd:
        year, month, day, rest = ymd.groups()
        hm = re.search(r"\b(\d{1,2}):(\d{2})\b", rest)
        text = f"{year}-{int(month):02d}-{int(day):02d}" + (f"T{int(hm[1]):02d}:{hm[2]}" if hm else "")

    # ISO dates are unambiguous; don't let DATE_ORDER=DMY reread 2026-10-02 as 10 Feb.
    if _ISO_RE.match(text.strip()):
        try:
            iso = datetime.fromisoformat(text.strip().replace("GMT", "").replace(" ", ""))
            has_time = "T" in text
            iso = iso.replace(tzinfo=zone) if iso.tzinfo is None else iso.astimezone(zone)
            return iso, has_time
        except ValueError:
            pass

    base = reference.astimezone(zone).replace(tzinfo=None)
    settings = {
        "RELATIVE_BASE": base,
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "DATE_ORDER": date_order,
    }
    parsed = _parse(text, settings)
    if parsed is None:
        return None, False

    # "Oct 1" in an email sent Oct 4 is an overdue date, not next year's.
    if not _YEAR_RE.search(text) and parsed.replace(tzinfo=None) - base > timedelta(days=180):
        parsed = _parse(text, {**settings, "PREFER_DATES_FROM": "past"}) or parsed

    has_time = bool(_TIME_RE.search(text))
    parsed = parsed.astimezone(zone)
    if not has_time:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return parsed, has_time


# --- how people write dates in chat ("tmr 5pm", "in 2 hours", "next monday at 9am") ---------

_SLANG = [(re.compile(r"\b(tmr|tmrw|tmw|tml|2moro|2mrw|tomoro|tomorow|tomorro)\b", re.I), "tomorrow"),
          (re.compile(r"\b(tdy|tday|tonight|tonite)\b", re.I), "today")]
_IN_RE = re.compile(r"^in\s+(\d+|an?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?|w|wks?|weeks?)$", re.I)
_CLOCK_RE = re.compile(r"(?:\bat\s+)?\b(?:(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?|(\d{1,2}):(\d{2})\b|(noon|midnight)\b)", re.I)


def unslang(text: str) -> str:
    for pattern, word in _SLANG:
        text = pattern.sub(word, text)
    return text


def resolve_spoken(text: str | None, now: datetime, tz: str, date_order: str = "DMY") -> tuple[datetime | None, bool]:
    """Like resolve(), for dates typed in chat. Reads the clock time separately from the day, since
    dateparser misreads "next monday at 9am" and puts a bare "8pm" on tomorrow even when it's still 3pm.
    """
    if not text or not text.strip():
        return None, False
    text = re.sub(r"^(?:on|at|by|this coming|coming)\s+", "", unslang(text.strip()), flags=re.I)
    zone = ZoneInfo(tz)
    now = now.astimezone(zone)
    inside = _IN_RE.match(text)
    if inside:
        n = 1 if inside[1].lower() in ("a", "an") else int(inside[1])
        unit = inside[2].lower()[0]
        if unit in "mh":
            return (now + timedelta(minutes=n if unit == "m" else 60 * n)).replace(second=0, microsecond=0), True
        day = now + timedelta(days=n if unit == "d" else 7 * n)
        return day.replace(hour=0, minute=0, second=0, microsecond=0), False
    clock = _CLOCK_RE.search(text)
    if clock is None or _ISO_RE.match(text):
        return resolve(text, now, tz, date_order)
    if clock[6]:
        hour, minute = (12, 0) if clock[6].lower() == "noon" else (0, 0)
    elif clock[3]:
        hour, minute = int(clock[1]) % 12 + (12 if clock[3].lower() == "p" else 0), int(clock[2] or 0)
    else:
        hour, minute = int(clock[4]), int(clock[5])
    if hour > 23 or minute > 59:
        return None, False
    rest = re.sub(r"^\s*(on|at|by)\s+|\s+(on|at|by)\s*$", "", (text[:clock.start()] + " " + text[clock.end():]).strip(" ,"))
    if rest.strip():
        day, _ = resolve(rest, now, tz, date_order)
        if day is None:
            return None, False
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0), True
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (at if at > now else at + timedelta(days=1)), True  # a bare time: today, or tomorrow if it's passed
