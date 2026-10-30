"""Hold back suggestions you'd dismiss anyway, so what Dorae-2 shows you is worth a look.

Of the first 138 things found in email, 119 were dismissed. Replaying them in order, three signals caught 76 of those
and only one you'd kept (shown below, still one tap away):
- a digest: one email with DIGEST or more things in it (a library's events newsletter, a schedule for every year
  group, seven escalator closures): 57 dismissed, none kept;
- mass mail: an unsubscribe link in its headers (job alerts, course promotions, weekly tasks);
- a sender you always dismiss: LEARN_AFTER dismissed and nothing ever confirmed.

Held items wait under "Held back" on the All items page with the usual Confirm and Dismiss, and stay out of "need your
OK", Telegram and the briefing. Never held: flights, hotels, deliveries, bills and your own reminders (missing one costs
more than a glance), anything from a sender you've confirmed something from, and your own forwards.
"""
from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.schema import ItemType
from doraemon.senders import address, name, records

HELD = "held"
HOLD = "hold: "  # action_log prefix, then why (shown on the card)
DIGEST = 3
LEARN_AFTER = 3
NEVER_HELD = {ItemType.FLIGHT, ItemType.HOTEL, ItemType.DELIVERY, ItemType.BILL, ItemType.REMINDER}


def why(db: Database, email: ParsedEmail, rows: list, own: tuple[str, ...] = ()) -> str | None:
    """Why the suggestions found in `email` (its saved `rows`) should wait under Held back, or None to show them."""
    sender = address(email.sender)
    if not sender or sender in {a.lower() for a in own}:
        return None
    seen = records(db).get(sender, {"dismissed": 0, "confirmed": 0})
    if seen["confirmed"]:
        return None
    if sum(1 for r in rows if r["type"] not in NEVER_HELD) >= DIGEST:
        return f"a newsletter or schedule, with {len(rows)} things in one email"
    if email.bulk:
        return "mass mail (it has an unsubscribe link)"
    if seen["dismissed"] >= LEARN_AFTER:
        return f"you've dismissed all {seen['dismissed']} things from {name(email.sender)}"
    return None


def hold(db: Database, gmail_id: str, email: ParsedEmail, own: tuple[str, ...] = ()) -> int:
    """Hold back what was just saved from this email, if it should wait (see `why`). Returns how many."""
    rows = db.conn.execute("SELECT * FROM action_items WHERE gmail_id = ? AND status = 'proposed' AND edited = 0",
                           (gmail_id,)).fetchall()
    reason = why(db, email, rows, own) if rows else None
    if reason is None:
        return 0
    held = [r for r in rows if r["type"] not in NEVER_HELD]
    for r in held:
        db.update("item", r["id"], HOLD + reason, status=HELD)
    return len(held)


def reason(db: Database, item_id: int) -> str:
    """Why this item was held back, for its card."""
    row = db.conn.execute("SELECT action FROM action_log WHERE kind = 'item' AND row_id = ? AND action LIKE ? "
                          "ORDER BY id DESC LIMIT 1", (item_id, HOLD + "%")).fetchone()
    return row["action"][len(HOLD):] if row else ""
