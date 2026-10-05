"""Confirmed items become events in a "Doraemon" Google Calendar, linked back to the email,
and Dorae-2 can show your week from all your calendars.

Doraemon only writes to its own calendar (scope calendar.app.created). Your
other calendars are read-only to it: it reads their events to show your week
and never changes them. Hide or delete the Doraemon calendar in Google Calendar any time.

    python -m doraemon.calendar_sync connect   # approve access (opens your browser)
    python -m doraemon.calendar_sync push      # add confirmed items that aren't in the calendar yet
"""
import argparse
import re
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from doraemon.config import Settings
from doraemon.db import Database, item_model
from doraemon.google_auth import ALL_SCOPES, CALENDAR, CALENDAR_READ, load_credentials
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


# --- reading your week ----------------------------------------------------------

HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


def schedule_event(e: dict, calendar: str, color: str, zone: ZoneInfo) -> dict | None:
    """One Google event as the week view shows it, or None if it shouldn't show (cancelled, declined)."""
    if e.get("status") == "cancelled" or e.get("eventType") == "workingLocation":
        return None
    if any(a.get("self") and a.get("responseStatus") == "declined" for a in e.get("attendees", [])):
        return None
    start, end = e.get("start", {}), e.get("end", {})
    if "date" in start:  # all day; Google's end date is exclusive
        first = date.fromisoformat(start["date"])
        last = date.fromisoformat(end.get("date", start["date"])) - timedelta(days=1)
        at = until = None
    elif "dateTime" in start:
        at = datetime.fromisoformat(start["dateTime"]).astimezone(zone)
        until = datetime.fromisoformat(end["dateTime"]).astimezone(zone) if "dateTime" in end else at
        first = at.date()
        last = (until - timedelta(microseconds=1)).date() if until > at else first  # ending at midnight stays on the day
    else:
        return None
    link = e.get("htmlLink", "")
    return {"title": e.get("summary") or "(No title)", "first": first.isoformat(), "last": max(first, last).isoformat(),
            "start": at.strftime("%H:%M") if at else "", "end": until.strftime("%H:%M") if until else "",
            "calendar": calendar, "color": color if HEX_COLOR.fullmatch(color or "") else "#9AA0A6",
            "location": e.get("location", ""), "link": link if link.startswith("https://") else ""}


class Schedule(Protocol):
    def events(self, first: date, last: date) -> list[dict]: ...


class GoogleSchedule:
    """Events from the calendars you show in Google Calendar. Read-only."""

    def __init__(self, creds, timezone: str) -> None:
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.zone = ZoneInfo(timezone)

    def calendars(self) -> list[dict]:
        found, page = [], None
        while True:
            resp = self.service.calendarList().list(pageToken=page).execute()
            found += [c for c in resp.get("items", []) if (c.get("primary") or c.get("selected")) and not c.get("hidden")]
            page = resp.get("nextPageToken")
            if not page:
                return found

    def events(self, first: date, last: date) -> list[dict]:
        """Events from the first to the last day (both included), all-day ones first each day."""
        t0 = datetime.combine(first, time(0), self.zone)
        t1 = datetime.combine(last + timedelta(days=1), time(0), self.zone)
        found = []
        for cal in self.calendars():
            name = cal.get("summaryOverride") or cal.get("summary", "")
            page = None
            while True:
                resp = self.service.events().list(
                    calendarId=cal["id"], timeMin=t0.isoformat(), timeMax=t1.isoformat(), singleEvents=True,
                    orderBy="startTime", maxResults=250, pageToken=page).execute()
                found += [ev for e in resp.get("items", [])
                          if (ev := schedule_event(e, name, cal.get("backgroundColor", ""), self.zone))]
                page = resp.get("nextPageToken")
                if not page:
                    break
        return sorted(found, key=lambda ev: (ev["first"], ev["start"]))


def connect_schedule(settings: Settings) -> GoogleSchedule:
    """Raises NotConnected if reading your calendars hasn't been approved yet. Never opens a browser."""
    creds = load_credentials(settings.google_credentials, settings.google_token, need=CALENDAR_READ, interactive=False)
    return GoogleSchedule(creds, settings.timezone)


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
        print(f"Connected. Confirmed items go to your '{CALENDAR_NAME}' calendar ({cal_id}),")
        print("and Dorae-2 can show your week from all your calendars (read-only).")
    added, skipped = push_confirmed(db, cal)
    print(f"Added {added} confirmed item(s) to the calendar" + (f"; {skipped} have no date." if skipped else "."))


if __name__ == "__main__":
    main()
