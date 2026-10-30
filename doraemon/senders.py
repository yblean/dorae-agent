"""Senders whose emails you never want: Doraemon offers a one-tap "Skip emails from ..." for them.

Most of what Dorae-2 suggests from some senders is dismissed (a library's newsletters, a school's
announcements): once you've dismissed OFFER_AFTER of their items and confirmed none, it offers to skip
their emails from now on (an ignore_sender rule, doraemon.rules: they're no longer read at all). It asks
once per sender, never for your own address (your forwards), and never decides for you.
"""
from email.utils import parseaddr

from doraemon.db import Database, from_chat
from doraemon.rules import RuleStore
from doraemon.tidy import HELD_EXPIRED, TWIN

OFFER_AFTER = 3
OFFERED = "skip-offered:"  # app_settings key prefix: asked about this sender already


def address(sender: str) -> str:
    return parseaddr(sender or "")[1].lower()


def name(sender: str) -> str:
    """'National Library Board' from 'National Library Board <edm@...>', or the address."""
    shown, addr = parseaddr(sender or "")
    return shown.strip(' "') or addr


def records(db: Database) -> dict[str, dict]:
    """Per sender address: {"sender", "dismissed", "confirmed", "item"} over every item found in their emails.
    Dismissals that only removed a repeat of another item, or a held-back one you never saw (doraemon.tidy), don't
    count against them."""
    twins = {r[0] for r in db.conn.execute(
        "SELECT row_id FROM action_log WHERE kind = 'item' AND action IN (?, ?) AND undone = 0", (TWIN, HELD_EXPIRED))}
    found: dict[str, dict] = {}
    for r in db.conn.execute("""SELECT i.id, i.status, i.gmail_id, e.sender FROM action_items i
                                JOIN processed_emails e ON e.message_id = i.gmail_id"""):
        if from_chat(r["gmail_id"]) or not address(r["sender"]):
            continue
        rec = found.setdefault(address(r["sender"]), {"sender": r["sender"], "dismissed": 0, "confirmed": 0, "item": r["id"]})
        rec["item"] = max(rec["item"], r["id"])  # the newest, to point a Telegram button at
        if r["status"] == "confirmed":
            rec["confirmed"] += 1
        elif r["status"] == "dismissed" and r["id"] not in twins:
            rec["dismissed"] += 1
    return found


def worth_skipping(db: Database, rules: RuleStore, own: tuple[str, ...] = (), sender: str | None = None) -> list[dict]:
    """Senders to offer skipping (or just `sender`, if it qualifies), most dismissed first: OFFER_AFTER or more
    dismissed, none confirmed, not you, not skipped already and not asked about before."""
    mine = {a.lower() for a in own}
    offers = []
    for addr, rec in records(db).items():
        if sender is not None and addr != address(sender):
            continue
        if rec["dismissed"] < OFFER_AFTER or rec["confirmed"] or addr in mine or rules.ignored_by(rec["sender"]) \
                or db.get_setting(OFFERED + addr):
            continue
        offers.append({**rec, "address": addr, "name": name(rec["sender"])})
    return sorted(offers, key=lambda o: -o["dismissed"])


def mark_offered(db: Database, offers: list[dict]) -> None:
    for o in offers:
        db.set_setting(OFFERED + o["address"], "1")


def skip(db: Database, rules: RuleStore, sender: str, created_from: str) -> tuple[str, int]:
    """Stop reading this sender's emails, and dismiss what's still waiting from them. Returns (what to say, how many
    waiting ones were dismissed)."""
    rule = rules.add("ignore_sender", sender, created_from=created_from)
    waiting = [r for r in db.items(("proposed",)) if address(r["sender"] or "") == address(sender)]
    for r in waiting:
        db.update("item", r["id"], "dismiss (ignored sender)", status="dismissed")
    extra = f" and dismissed the {len(waiting)} still waiting" if waiting else ""
    return (f"Done: I won't read emails from {name(sender)} ({rule.match}) any more{extra}. "
            "Undo it under Learned rules."), len(waiting)


def offer_text(offers: list[dict]) -> str:
    if len(offers) == 1:
        o = offers[0]
        return (f"You've dismissed all {o['dismissed']} things I found in emails from {o['name']}. "
                "Want me to skip their emails from now on?")
    names = ", ".join(f"{o['name']} ({o['dismissed']})" for o in offers[:3])
    more = f" and {len(offers) - 3} more" if len(offers) > 3 else ""
    return (f"You've dismissed everything I found in emails from {len(offers)} senders: {names}{more}. "
            "Want me to skip their emails from now on? Pick the ones below.")
