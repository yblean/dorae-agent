"""Local database. Milestone 4 needs only processed_emails; items and the action log come in milestone 5.

Email bodies are never stored, only ids and what happened to each email.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Database:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id TEXT PRIMARY KEY,   -- Gmail's id
                thread_id TEXT,
                triage_label TEXT NOT NULL,    -- 'extracted', 'nothing found', or 'skipped: <why>'
                processed_at TEXT NOT NULL
            )""")
        self.conn.commit()

    def is_processed(self, message_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)).fetchone()
        return row is not None

    def mark_processed(self, message_id: str, thread_id: str, triage_label: str) -> None:
        self.conn.execute(
            """INSERT INTO processed_emails (message_id, thread_id, triage_label, processed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (message_id) DO UPDATE SET
                 triage_label = excluded.triage_label, processed_at = excluded.processed_at""",
            (message_id, thread_id, triage_label, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
