"""Local database: processed emails, proposed items, transactions and the action log.

Email bodies are never stored: only ids, subject, sender and what was extracted
(evidence snippets are capped at 200 characters).
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from doraemon.email_parse import ParsedEmail
from doraemon.schema import ActionItem, Extraction, Transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id TEXT PRIMARY KEY,   -- Gmail's id
    thread_id TEXT,
    triage_label TEXT NOT NULL,    -- 'extracted', 'nothing found', or 'skipped: <why>'
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY,
    gmail_id TEXT NOT NULL,
    type TEXT NOT NULL, title TEXT NOT NULL,
    start_at TEXT, end_at TEXT, all_day INTEGER NOT NULL, timezone TEXT NOT NULL,
    departs_from TEXT, location TEXT, booking_ref TEXT,
    amount TEXT, currency TEXT, date_text TEXT,
    evidence_snippet TEXT NOT NULL, confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',   -- proposed / confirmed / dismissed
    edited INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    gmail_id TEXT NOT NULL,
    source TEXT NOT NULL, merchant TEXT NOT NULL, order_ref TEXT,
    purchased_at TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL, amount_home TEXT,
    category TEXT NOT NULL, is_refund INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'counted',    -- counted / removed
    edited INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- 'item' or 'transaction'
    row_id INTEGER NOT NULL,
    action TEXT NOT NULL,          -- confirm, dismiss, edit, remove, restore, ...
    before_state TEXT NOT NULL,
    after_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,           -- 'money' (Dorae-1) or 'calendar' (Dorae-2)
    role TEXT NOT NULL,            -- 'agent' or 'user'
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',   -- text / breakdown / payments / items / agenda
    payload TEXT,                  -- JSON for the card, e.g. which items or payments it shows
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_types (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- shown on the card, e.g. 'Study'
    base TEXT NOT NULL             -- the built-in type it works like: 'reminder' (Telegram) or 'appointment' (Calendar)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    remind_at TEXT NOT NULL,       -- UTC
    label TEXT NOT NULL,           -- e.g. '1 day before'
    status TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / missed / cancelled
    sent_at TEXT
);
"""
# Columns added after a table's first version: table -> {column: type}
_ADDED_COLUMNS = {
    "processed_emails": {"subject": "TEXT", "sender": "TEXT", "sent_at": "TEXT", "notes": "TEXT"},
    "action_items": {"calendar_event_id": "TEXT", "custom_type_id": "INTEGER"},
}
_TABLES = {"item": "action_items", "transaction": "transactions"}
CHAT_ID_PREFIX = "chat:"  # gmail_id of items you asked for in chat rather than found in an email


def from_chat(gmail_id: str) -> bool:
    return gmail_id.startswith(CHAT_ID_PREFIX)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.conn.commit()

    # --- small key/value settings (e.g. the Doraemon calendar's id) ----------

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # --- agent chats ------------------------------------------------------------

    def add_message(self, agent: str, role: str, text: str, kind: str = "text", payload: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO chat_messages (agent, role, text, kind, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, role, text, kind, json.dumps(payload) if payload is not None else None, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def messages(self, agent: str, limit: int = 80, after_id: int = 0) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM chat_messages WHERE agent = ? AND id > ? ORDER BY id DESC LIMIT ?", (agent, after_id, limit)
        ).fetchall()
        return list(reversed(rows))

    def last_message(self, agent: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM chat_messages WHERE agent = ? ORDER BY id DESC LIMIT 1", (agent,)
        ).fetchone()

    def clear_messages(self, agent: str) -> None:
        self.conn.execute("DELETE FROM chat_messages WHERE agent = ?", (agent,))
        self.conn.commit()

    def set_calendar_event(self, item_id: int, event_id: str | None) -> None:
        """Not a user action, so not in the action log: undoing the confirm removes the event."""
        self.conn.execute("UPDATE action_items SET calendar_event_id = ? WHERE id = ?", (event_id, item_id))
        self.conn.commit()

    # --- your own item types ('Study', 'Gym'), each working like a built-in one ----

    def custom_types(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM custom_types ORDER BY name COLLATE NOCASE").fetchall()

    def custom_type(self, type_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM custom_types WHERE id = ?", (type_id,)).fetchone()

    def add_custom_type(self, name: str, base: str) -> sqlite3.Row:
        """The type with this name (any case), made if it's new. An existing one keeps how it works."""
        self.conn.execute("INSERT OR IGNORE INTO custom_types (name, base) VALUES (?, ?)", (name, base))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM custom_types WHERE name = ?", (name,)).fetchone()

    def delete_custom_type(self, type_id: int) -> None:
        """Items of this type keep working as the built-in type it was based on."""
        self.conn.execute("UPDATE action_items SET custom_type_id = NULL WHERE custom_type_id = ?", (type_id,))
        self.conn.execute("DELETE FROM custom_types WHERE id = ?", (type_id,))
        self.conn.commit()

    # --- Telegram reminders (not in the action log: undoing the confirm cancels them) ---

    def set_reminders(self, item_id: int, planned: list[dict]) -> None:
        """Replace the item's unsent reminders with `planned` ([{"at": datetime, "label": str}])."""
        self.conn.execute("DELETE FROM reminders WHERE item_id = ? AND status = 'pending'", (item_id,))
        for r in planned:
            self.conn.execute("INSERT INTO reminders (item_id, remind_at, label) VALUES (?, ?, ?)",
                              (item_id, r["at"].astimezone(timezone.utc).isoformat(), r["label"]))
        self.conn.commit()

    def reminders(self, item_id: int, status: str = "pending") -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM reminders WHERE item_id = ? AND status = ? ORDER BY remind_at",
                                 (item_id, status)).fetchall()

    def upcoming_reminders(self) -> list[sqlite3.Row]:
        """Every unsent reminder of a confirmed item, soonest first."""
        return self.conn.execute(
            """SELECT r.* FROM reminders r JOIN action_items i ON i.id = r.item_id
               WHERE r.status = 'pending' AND i.status = 'confirmed' ORDER BY r.remind_at""").fetchall()

    def due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        """Unsent reminders whose time has come, oldest first."""
        return self.conn.execute("SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= ? ORDER BY remind_at",
                                 (now.astimezone(timezone.utc).isoformat(),)).fetchall()

    def mark_reminder(self, reminder_id: int, status: str) -> None:
        self.conn.execute("UPDATE reminders SET status = ?, sent_at = ? WHERE id = ?",
                          (status, _now() if status == "sent" else None, reminder_id))
        self.conn.commit()

    def cancel_reminders(self, item_id: int) -> int:
        cur = self.conn.execute("UPDATE reminders SET status = 'cancelled' WHERE item_id = ? AND status = 'pending'",
                                (item_id,))
        self.conn.commit()
        return cur.rowcount

    # --- ingest -------------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)).fetchone()
        return row is not None

    def mark_processed(self, message_id: str, thread_id: str, triage_label: str) -> None:
        self.conn.execute(
            """INSERT INTO processed_emails (message_id, thread_id, triage_label, processed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (message_id) DO UPDATE SET
                 triage_label = excluded.triage_label, processed_at = excluded.processed_at""",
            (message_id, thread_id, triage_label, _now()),
        )
        self.conn.commit()

    def save_result(self, gmail_id: str, thread_id: str, email: ParsedEmail,
                    result: Extraction, triage_label: str) -> None:
        """Store what the pipeline found. Reprocessing replaces untouched proposals,
        but never anything you confirmed, dismissed or edited."""
        self.mark_processed(gmail_id, thread_id, triage_label)
        notes = "\n".join(result.applied_rules + result.problems)
        self.conn.execute(
            "UPDATE processed_emails SET subject = ?, sender = ?, sent_at = ?, notes = ? WHERE message_id = ?",
            (email.subject, email.sender, email.sent_at.isoformat(), notes, gmail_id),
        )
        self.conn.execute("DELETE FROM action_items WHERE gmail_id = ? AND status = 'proposed' AND edited = 0",
                          (gmail_id,))
        self.conn.execute("DELETE FROM transactions WHERE gmail_id = ? AND status = 'counted' AND edited = 0",
                          (gmail_id,))
        for item in result.items:
            row = item.model_dump(mode="json", exclude={"message_id", "status"})
            row["gmail_id"] = gmail_id
            self._insert("action_items", row)
        for txn in result.transactions:
            row = txn.model_dump(mode="json", exclude={"message_id"})
            row["gmail_id"] = gmail_id
            self._insert("transactions", row)
        self.conn.commit()

    def add_item(self, source_id: str, item: ActionItem) -> int:
        """An item that didn't come from an email, e.g. an event you asked Dorae-2 to add. Returns its id."""
        row = item.model_dump(mode="json", exclude={"message_id", "status"})
        row["gmail_id"] = source_id
        item_id = self._insert("action_items", row)
        self.conn.commit()
        return item_id

    def _insert(self, table: str, row: dict) -> int:
        row = {k: (int(v) if isinstance(v, bool) else v) for k, v in row.items()}
        cols = ", ".join(row)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})",
                                list(row.values()))
        return cur.lastrowid

    # --- reading ------------------------------------------------------------

    def items(self, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"""SELECT i.*, e.subject, e.sender, e.sent_at FROM action_items i
                LEFT JOIN processed_emails e ON e.message_id = i.gmail_id
                WHERE i.status IN ({marks})
                ORDER BY COALESCE(i.start_at, e.sent_at)""",
            statuses,
        ).fetchall()

    def transactions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT t.*, e.subject, e.sender FROM transactions t
               LEFT JOIN processed_emails e ON e.message_id = t.gmail_id
               ORDER BY t.purchased_at DESC"""
        ).fetchall()

    def get(self, kind: str, row_id: int) -> sqlite3.Row | None:
        return self.conn.execute(f"SELECT * FROM {_TABLES[kind]} WHERE id = ?", (row_id,)).fetchone()

    def recent_actions(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM action_log WHERE undone = 0 ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM action_items GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # --- changing (every change is logged and undoable) ---------------------

    def update(self, kind: str, row_id: int, action: str, **changes) -> None:
        before = self.get(kind, row_id)
        if before is None:
            raise KeyError(f"no {kind} #{row_id}")
        before_state = {k: before[k] for k in changes}
        sets = ", ".join(f"{k} = ?" for k in changes)
        self.conn.execute(f"UPDATE {_TABLES[kind]} SET {sets} WHERE id = ?", [*changes.values(), row_id])
        self.conn.execute(
            """INSERT INTO action_log (kind, row_id, action, before_state, after_state, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, row_id, action, json.dumps(before_state), json.dumps(changes), _now()),
        )
        self.conn.commit()

    def undo(self, log_id: int) -> sqlite3.Row:
        entry = self.conn.execute("SELECT * FROM action_log WHERE id = ? AND undone = 0", (log_id,)).fetchone()
        if entry is None:
            raise KeyError(f"no undoable action #{log_id}")
        before = json.loads(entry["before_state"])
        sets = ", ".join(f"{k} = ?" for k in before)
        self.conn.execute(f"UPDATE {_TABLES[entry['kind']]} SET {sets} WHERE id = ?",
                          [*before.values(), entry["row_id"]])
        self.conn.execute("UPDATE action_log SET undone = 1 WHERE id = ?", (log_id,))
        self.conn.commit()
        return entry


def item_model(row: sqlite3.Row) -> ActionItem:
    data = {k: row[k] for k in row.keys() if k in ActionItem.model_fields}
    return ActionItem(message_id=row["gmail_id"], **data)


def transaction_model(row: sqlite3.Row) -> Transaction:
    data = {k: row[k] for k in row.keys() if k in Transaction.model_fields}
    return Transaction(message_id=str(row["id"]), **data)
