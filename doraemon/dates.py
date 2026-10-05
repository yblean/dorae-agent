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
