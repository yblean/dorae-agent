"""Keeping "needs your OK" short: the same thing found in two emails, and suggestions that passed unanswered.

- A forwarded email, or a booking's confirmation and its e-ticket, gives the same flight or hotel twice.
  find_twin() spots it when a new email is read (doraemon.ingest skips it), and dismiss_twins() clears
  any already waiting.
- A suggestion still waiting a day after its date is over (a survey deadline in September, last week's
  gameweek) won't be confirmed any more: expire_passed() dismisses it.

Both are dismissals in Activity, so each one can be undone there.
"""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from doraemon.db import Database, item_model
from doraemon.ledger import name_tokens
from doraemon.schema import ActionItem

EXPIRE_AFTER = timedelta(days=1)  # how long past its last day a waiting suggestion is kept
TWIN, EXPIRED = "dismiss (same as another item)", "dismiss (passed without your OK)"


def _day(item: ActionItem, at: datetime | None) -> date | None:
    return at.astimezone(ZoneInfo(item.timezone)).date() if at else None


def same_item(a: ActionItem, b: ActionItem) -> bool:
    """The same flight, booking or deadline told twice: same type and day (and time, for timed ones), and the same
    booking ref, a shared number-bearing word (MU2048, an order number) or mostly the same title words."""
    if a.type != b.type or a.start_at is None or b.start_at is None or _day(a, a.start_at) != _day(b, b.start_at):
        return False
    if not (a.all_day or b.all_day) and a.start_at != b.start_at:
        return False
    if a.booking_ref and b.booking_ref:
        return a.booking_ref.strip().lower() == b.booking_ref.strip().lower()
    wa, wb = name_tokens(a.title), name_tokens(b.title)
    if any(re.search(r"\d", w) for w in wa & wb):
        return True
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def find_twin(db: Database, item: ActionItem, gmail_id: str):
    """An item from another email that's the same as `item` (whatever you did with it), or None."""
    for row in db.items(("proposed", "confirmed", "dismissed")):
        if row["gmail_id"] != gmail_id and same_item(item_model(row), item):
            return row
    return None


def dismiss_twins(db: Database) -> list[str]:
    """Dismiss waiting suggestions that repeat an earlier item. Returns their titles."""
    rows = db.items(("proposed", "confirmed"))
    kept, cleared = [], []
    for row in sorted(rows, key=lambda r: (r["status"] != "confirmed", r["id"])):  # confirmed ones, then oldest, win
        item = item_model(row)
        if row["status"] == "proposed" and any(same_item(item_model(k), item) for k in kept):
            db.update("item", row["id"], TWIN, status="dismissed")
            cleared.append(row["title"])
        else:
            kept.append(row)
    return cleared


def expire_passed(db: Database, user_tz: str, now: datetime) -> list[str]:
    """Dismiss suggestions still waiting more than EXPIRE_AFTER after their last day. Returns their titles."""
    today = now.astimezone(ZoneInfo(user_tz)).date()
    cleared = []
    for row in db.items(("proposed",)):
        item = item_model(row)
        days = [d for d in (_day(item, item.start_at), _day(item, item.end_at)) if d]
        last = max(days) if days else None  # the later one: a misread end date mustn't make it look over
        if last is not None and last + EXPIRE_AFTER < today:
            db.update("item", row["id"], EXPIRED, status="dismissed")
            cleared.append(row["title"])
    return cleared


def tidy(db: Database, user_tz: str, now: datetime, expire: bool = True) -> str:
    """Both, as one sentence for Dorae-2's chat; "" when there was nothing to clear."""
    twins = dismiss_twins(db)
    passed = expire_passed(db, user_tz, now) if expire else []
    parts = []
    if twins:
        parts.append(f"{len(twins)} that repeated another item ({_titles(twins)})")
    if passed:
        parts.append(f"{len(passed)} that passed without your OK ({_titles(passed)})")
    if not parts:
        return ""
    return "🧹 I cleared " + " and ".join(parts) + ". Undo any of them under Activity."


def _titles(titles: list[str], n: int = 3) -> str:
    shown = ", ".join(f"“{t}”" for t in titles[:n])
    return shown + (f" and {len(titles) - n} more" if len(titles) > n else "")
