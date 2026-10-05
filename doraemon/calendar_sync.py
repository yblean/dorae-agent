"""Confirmed items become events in a "Doraemon" Google Calendar, linked back to the email.

Doraemon creates and only touches its own calendar (scope calendar.app.created),
so your other calendars are never read or changed. Hide or delete the Doraemon
calendar in Google Calendar any time.

    python -m doraemon.calendar_sync connect   # approve access (opens your browser)
    python -m doraemon.calendar_sync push      # add confirmed items that aren't in the calendar yet
"""
import argparse
from datetime import timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from doraemon.config import Settings
from doraemon.db import Database, item_model
from doraemon.google_auth import ALL_SCOPES, CALENDAR, load_credentials
from doraemon.schema import ActionItem, ItemType

CALENDAR_NAME = "Doraemon"
EMOJI = {"bill": "💸", "appointment": "📌", "deadline": "⏰", "delivery": "📦", "rsvp": "✉️",
         "flight": "✈️", "hotel": "🏨", "other_travel": "🚆"}
ALL_DAY_TYPES = {ItemType.BILL, ItemType.DELIVERY, ItemType.HOTEL}
DEFAULT_MINUTES = {ItemType.FLIGHT: 120, ItemType.OTHER_TRAVEL: 90, ItemType.DEADLINE: 30}
# All-day reminders count back from midnight: 900 min = 9am the day before, 3780 = 9am three days before
ALL_DAY_REMINDERS = [{"method": "popup", "minutes": 900}, {"method": "popup", "minutes": 3780}]
TIMED_REMINDERS = [{"method": "popup", "minutes": 60}, {"method": "popup", "minutes": 1440}]


def gmail_link(gmail_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{gmail_id}"


def event_body(item: ActionItem, item_id: int, gmail_id: str) -> dict:
    """The Google Calendar event for one confirmed item. The item must have a date."""
    if item.start_at is None:
        raise ValueError("item has no date")
    zone = ZoneInfo(item.timezone)
    start = item.start_at.astimezone(zone)
    lines = [item.type.value.replace("_", " ").capitalize()]
    if item.amount is not None:
        lines.append(f"Amount: {item.currency} {item.amount}")
    if item.location:
        lines.append(f"Where: {item.location}")
    if item.departs_from:
        lines.append(f"Departs: {item.departs_from}")
    if item.booking_ref:
        lines.append(f"Booking ref: {item.booking_ref}")
    if item.evidence_snippet:
        lines.append(f"From the email: “{item.evidence_snippet}”")
    lines += ["", f"Open the email: {gmail_link(gmail_id)}", "Added by Doraemon after you confirmed it."]

    body = {
        "summary": f"{EMOJI.get(item.type.value, '•')} {item.title}",
        "description": "\n".join(lines),
        "source": {"title": "Open email in Gmail", "url": gmail_link(gmail_id)},
        "extendedProperties": {"private": {"doraemon_item_id": str(item_id)}},
    }
    if item.location:
        body["location"] = item.location

    if item.all_day or item.type in ALL_DAY_TYPES:
        first = start.date()
        last = item.end_at.astimezone(zone).date() if item.end_at else first  # hotel: through check-out
        body["start"] = {"date": first.isoformat()}
        body["end"] = {"date": (max(last, first) + timedelta(days=1)).isoformat()}  # end date is exclusive
        body["reminders"] = {"useDefault": False, "overrides": ALL_DAY_REMINDERS}
    else:
        end = item.end_at.astimezone(zone) if item.end_at and item.end_at > item.start_at else \
            start + timedelta(minutes=DEFAULT_MINUTES.get(item.type, 60))
        body["start"] = {"dateTime": start.isoformat(), "timeZone": item.timezone}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": item.timezone}
        body["reminders"] = {"useDefault": False, "overrides": TIMED_REMINDERS}
    return body


class Calendar(Protocol):
    def upsert(self, event_id: str | None, body: dict) -> str: ...
    def delete(self, event_id: str) -> None: ...


class GoogleCalendar:
    def __init__(self, creds, db: Database, timezone: str) -> None:
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.db, self.timezone = db, timezone

    def calendar_id(self, create: bool = True) -> str:
        cal_id = self.db.get_setting("calendar_id")
        if cal_id or not create:
            return cal_id
        cal = self.service.calendars().insert(body={
            "summary": CALENDAR_NAME, "timeZone": self.timezone,
            "description": "Bills, appointments, deadlines and trips Doraemon found in your email.",
        }).execute()
        self.db.set_setting("calendar_id", cal["id"])
        return cal["id"]

    def upsert(self, event_id: str | None, body: dict) -> str:
        for attempt in range(2):
            cal_id = self.calendar_id()
            try:
                events = self.service.events()
                if event_id:
                    try:
                        return events.update(calendarId=cal_id, eventId=event_id, body=body).execute()["id"]
                    except HttpError as e:
                        if e.status_code not in (404, 410):
                            raise
                return events.insert(calendarId=cal_id, body=body).execute()["id"]
            except HttpError as e:
                if e.status_code == 404 and attempt == 0:  # you deleted the Doraemon calendar: make a new one
                    self.db.set_setting("calendar_id", None)
                    event_id = None
                    continue
                raise
        raise RuntimeError("unreachable")

    def delete(self, event_id: str) -> None:
        cal_id = self.calendar_id(create=False)
        if not cal_id:
            return
        try:
            self.service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        except HttpError as e:
            if e.status_code not in (404, 410):  # already gone is fine
                raise


def connect_calendar(settings: Settings, db: Database, interactive: bool = False) -> GoogleCalendar:
    """Raises NotConnected (when not interactive) if Calendar access hasn't been approved yet."""
    need = ALL_SCOPES if interactive else [CALENDAR]
    creds = load_credentials(settings.google_credentials, settings.google_token, need=need, interactive=interactive)
    return GoogleCalendar(creds, db, settings.timezone)


def push_confirmed(db: Database, cal: Calendar) -> tuple[int, int]:
    """Add confirmed items that aren't in the calendar yet. Returns (added, skipped without a date)."""
    added = skipped = 0
    for row in db.items(("confirmed",)):
        if row["calendar_event_id"]:
            continue
        item = item_model(row)
        if item.start_at is None:
            skipped += 1
            continue
        db.set_calendar_event(row["id"], cal.upsert(None, event_body(item, row["id"], row["gmail_id"])))
        added += 1
    return added, skipped


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m doraemon.calendar_sync")
    ap.add_argument("command", choices=["connect", "push"])
    args = ap.parse_args()
    settings = Settings()
    db = Database(settings.db_path)
    cal = connect_calendar(settings, db, interactive=True)
    cal_id = cal.calendar_id()
    if args.command == "connect":
        print(f"Connected. Confirmed items go to your '{CALENDAR_NAME}' calendar ({cal_id}).")
    added, skipped = push_confirmed(db, cal)
    print(f"Added {added} confirmed item(s) to the calendar" + (f"; {skipped} have no date." if skipped else "."))


if __name__ == "__main__":
    main()
