"""Read-only Gmail access.

The only scope requested is gmail.readonly: Doraemon can never send, delete,
archive or label mail. The first run opens your browser to approve access;
the token is saved locally (data/google/token.json) and refreshed after that.
"""
import base64
from dataclasses import dataclass
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def connect(credentials_path: str | Path, token_path: str | Path):
    credentials_path, token_path = Path(credentials_path), Path(token_path)
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # revoked, or expired (testing-mode apps expire tokens after 7 days)
    if not creds or not creds.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"No Google OAuth client at {credentials_path}. See 'Connect Gmail' in the README."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)  # opens your browser to approve read-only access

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


@dataclass
class GmailMessage:
    id: str
    thread_id: str
    label_ids: list[str]
    raw: bytes  # the full RFC 822 email, same as a downloaded .eml


def list_ids(service, query: str, limit: int = 0) -> list[str]:
    """Message ids matching a Gmail search, newest first."""
    ids: list[str] = []
    page = None
    while True:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page, maxResults=500).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page or (limit and len(ids) >= limit):
            return ids[:limit] if limit else ids


def fetch(service, message_id: str) -> GmailMessage:
    msg = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    return GmailMessage(
        id=msg["id"],
        thread_id=msg["threadId"],
        label_ids=msg.get("labelIds", []),
        raw=base64.urlsafe_b64decode(msg["raw"]),
    )
