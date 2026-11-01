"""How items read on a review card.

Travel times are shown in the event's own timezone with the user's time next to
it, so a wrong timezone guess ("Changi, 16:50 Japan time") is easy to spot and fix.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from doraemon.schema import ActionItem

_ZONE_NAMES = {
    "Asia/Singapore": "Singapore", "Asia/Tokyo": "Japan", "Asia/Shanghai": "China",
    "Asia/Hong_Kong": "Hong Kong", "Asia/Taipei": "Taiwan", "Asia/Seoul": "Korea",
    "Asia/Kuala_Lumpur": "Malaysia", "Asia/Bangkok": "Thailand", "Asia/Jakarta": "Jakarta",
    "Europe/London": "UK", "Australia/Sydney": "Sydney",
}


def zone_name(tz: str) -> str:
    return _ZONE_NAMES.get(tz, tz.split("/")[-1].replace("_", " "))


def _fmt(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b %Y}, {dt:%H:%M}"


def describe_when(item: ActionItem, user_tz: str) -> str:
    if item.start_at is None:
        return "No date found"
    start = item.start_at
    if item.all_day:
        last = item.end_at.astimezone(start.tzinfo) if item.end_at else start
        if last.date() <= start.date():
            return f"{start:%a} {start.day} {start:%b %Y} (all day)"
        first = f"{start:%a} {start.day}" + ("" if (start.year, start.month) == (last.year, last.month) else
                                             f" {start:%b}" if start.year == last.year else f" {start:%b %Y}")
        return f"{first} – {last:%a} {last.day} {last:%b %Y} ({(last.date() - start.date()).days + 1} days)"
    text = _fmt(start) + f" {zone_name(item.timezone)} time"
    if item.end_at and item.end_at > start:
        end = item.end_at.astimezone(start.tzinfo)
        text = text.replace(f", {start:%H:%M}", f", {start:%H:%M}–{end:%H:%M}") if end.date() == start.date() else \
            text + f" until {end:%a} {end.day} {end:%b}, {end:%H:%M}"
    if item.timezone != user_tz:
        local = item.start_at.astimezone(ZoneInfo(user_tz))
        text += f" ({local:%H:%M} {zone_name(user_tz)} time"
        text += f", {local.day} {local:%b})" if local.date() != item.start_at.date() else ")"
    if item.departs_from:
        text = f"Departs {item.departs_from}, " + text
    return text
