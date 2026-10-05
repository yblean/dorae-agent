"""Read-only Gmail access.

Doraemon only ever reads mail (gmail.readonly): it can never send, delete,
archive or label. Sign-in is shared with Calendar; see doraemon.google_auth.
"""
import base64
from dataclasses import dataclass
from pathlib import Path

from googleapiclient.discovery import build

from doraemon.google_auth import GMAIL, load_credentials


def connect(credentials_path: str | Path, token_path: str | Path, interactive: bool = True):
    creds = load_credentials(credentials_path, token_path, need=[GMAIL], interactive=interactive)
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
