"""One Google sign-in for everything Doraemon uses.

Scopes are the narrowest that work:
- gmail.readonly: read mail; can never send, delete or label
- calendar.app.created: create and change events only in calendars this app
  creates (the "Doraemon" calendar)
- calendar.events.readonly + calendar.calendarlist.readonly: see your other
  calendars' events to show your week; can never change them
"""
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR = "https://www.googleapis.com/auth/calendar.app.created"
CALENDAR_READ = ["https://www.googleapis.com/auth/calendar.events.readonly",
                 "https://www.googleapis.com/auth/calendar.calendarlist.readonly"]
ALL_SCOPES = [GMAIL, CALENDAR, *CALENDAR_READ]


class NotConnected(Exception):
    """The saved sign-in doesn't cover what's needed, and we may not open a browser here."""


def load_credentials(credentials_path: str | Path, token_path: str | Path,
                     need: list[str], interactive: bool = True) -> Credentials:
    credentials_path, token_path = Path(credentials_path), Path(token_path)
    creds = Credentials.from_authorized_user_file(str(token_path)) if token_path.exists() else None

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # revoked, or expired (testing-mode apps expire tokens after 7 days)
    if creds and creds.valid and creds.has_scopes(need):
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not interactive:
        raise NotConnected("Google access needs approving: run python -m doraemon.calendar_sync connect")
    if not credentials_path.exists():
        raise FileNotFoundError(f"No Google OAuth client at {credentials_path}. See 'Connect Gmail' in the README.")
    # Ask for everything at once, so one approval covers Gmail and Calendar
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), sorted({*ALL_SCOPES, *need}))
    creds = flow.run_local_server(port=0)  # opens your browser
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds
