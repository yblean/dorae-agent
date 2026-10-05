"""Milestone 4: pull new Gmail, run the pipeline, print what it found.

    python -m doraemon.ingest                 # emails since the last check (first time: last 30 days)
    python -m doraemon.ingest --days 7 --limit 20
    python -m doraemon.ingest --again         # reprocess emails already seen

The web app runs the same check every 15 minutes while it's open.

Read-only: nothing is written to Gmail or your calendar. Results go into the local
database (data/doraemon.db) for the review page: python -m doraemon.web
"""
import argparse
import dataclasses
from collections.abc import Callable
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from googleapiclient.errors import HttpError

from doraemon.config import Settings
from doraemon.db import Database, transaction_model
from doraemon.display import describe_when
from doraemon.email_parse import ParsedEmail, gmail_categories, parse_eml
from doraemon.gmail import GmailMessage, connect, fetch, list_ids
from doraemon.fx import FxRates
from doraemon.ledger import build_ledger, spending_totals
from doraemon.llm import Backend, get_backend
from doraemon.pipeline import process
from doraemon.rules import RuleStore
from doraemon.schema import Extraction


def ingest_message(msg: GmailMessage, backend: Backend, settings: Settings,
                   rules: RuleStore) -> tuple[ParsedEmail, Extraction]:
    email = parse_eml(msg.raw)
    # Gmail's own labels are more reliable than the Takeout header the parser reads
    email = dataclasses.replace(email, gmail_categories=gmail_categories(msg.label_ids))
    result = process(email, backend, settings, rules)
    return email, result


def triage_label(result: Extraction) -> str:
    if result.skipped:
        return f"skipped: {result.skipped}"
    return "extracted" if (result.items or result.transactions) else "nothing found"


def print_result(subject: str, result: Extraction, settings: Settings,
                 say: Callable[[str], None] = print) -> None:
    say(f"\n✉  {subject[:90]}")
    for item in result.items:
        amount = f"  {item.currency} {item.amount}" if item.amount else ""
        say(f"   [{item.type.value}] {item.title} | {describe_when(item, settings.timezone)}{amount}")
    for txn in result.transactions:
        refund = " (refund)" if txn.is_refund else ""
        say(f"   [spend] {txn.currency} {txn.amount}{refund}  {txn.merchant}  ({txn.category.value})")
    for note in result.applied_rules + result.problems:
        say(f"   · {note}")


FIRST_RUN_DAYS = 30
OVERLAP = timedelta(hours=1)  # mail can land in Gmail late; emails already read are skipped anyway
CHECKED_AT = "gmail_checked_at"


def search_window(db: Database, days: int | None, again: bool) -> tuple[str, str]:
    """The Gmail search for this run, and how to describe it.

    Without --days, only mail since the last complete check (looking back at most 30 days).
    """
    last = db.get_setting(CHECKED_AT)
    if days is None and last and not again:
        since = max(datetime.fromisoformat(last) - OVERLAP, datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS))
        return f"after:{int(since.timestamp())} -in:chats", "since the last check"
    days = days or FIRST_RUN_DAYS
    return f"newer_than:{days}d -in:chats", f"in the last {days} days"


def run_ingest(settings: Settings, days: int | None = None, limit: int = 0, again: bool = False,
               say: Callable[[str], None] = print, interactive: bool = True) -> dict:
    """Fetch new Gmail, run the pipeline on emails not seen before, save results.

    Used by the command line, the web page's "Run now" button and its 15-minute check.
    Raises NotConnected if not interactive and Google access needs approving.
    """
    started = datetime.now(timezone.utc)
    service = connect(settings.google_credentials, settings.google_token, interactive=interactive)
    account = service.users().getProfile(userId="me").execute()["emailAddress"].lower()
    settings = dataclasses.replace(settings, user_emails=tuple({*settings.user_emails, account}))
    db = Database(settings.db_path)
    rules = RuleStore(settings.db_path)
    backend = get_backend(settings.model, settings)

    query, window = search_window(db, days, again)
    ids = list_ids(service, query, limit)
    todo = [i for i in reversed(ids) if again or not db.is_processed(i)]  # oldest first
    say(f"{len(ids)} emails {window}, {len(todo)} to process with {backend.name}")

    counts: dict[str, int] = defaultdict(int)
    transactions = []
    found = 0
    stopped = ""
    for n, message_id in enumerate(todo, 1):
        try:
            msg = fetch(service, message_id)
            email, result = ingest_message(msg, backend, settings, rules)
        except httpx.ConnectError:  # the model isn't running: the rest would fail the same way
            stopped = f"couldn't reach the model ({backend.name}). Is it running?"
            say(f"Stopped: {stopped}")
            counts["failed"] += len(todo) - n + 1
            break
        except (HttpError, httpx.HTTPError) as e:
            say(f"[{n}/{len(todo)}] failed, will retry next run: {e}")
            counts["failed"] += 1
            continue
        label = triage_label(result)
        counts[label.split(":")[0]] += 1
        db.save_result(msg.id, msg.thread_id, email, result, label)
        transactions += result.transactions
        found += len(result.items)
        if result.items or result.transactions:
            print_result(email.subject, result, settings, say)  # the web app's checks stay quiet
        else:
            say(f"[{n}/{len(todo)}] {label}: {email.subject[:70]}")
    if settings.convert_currencies:
        warm_exchange_rates(db, settings)
    if not counts["failed"] and not (limit and len(ids) >= limit):
        db.set_setting(CHECKED_AT, started.isoformat())  # next time, only ask Gmail for newer mail
    return {"processed": len(todo) - counts["failed"], "items": found, "transactions": transactions,
            "counts": dict(counts), "stopped": stopped}


def warm_exchange_rates(db: Database, settings: Settings) -> None:
    """Fetch rates for overseas payments now, so the review page can convert them offline."""
    fx = FxRates(settings.db_path)
    for row in db.transactions():
        if row["currency"] != settings.home_currency and row["status"] == "counted":
            fx.to_home(transaction_model(row), settings.home_currency)


def main() -> None:
    settings = Settings()
    ap = argparse.ArgumentParser(prog="python -m doraemon.ingest")
    ap.add_argument("--days", type=int, default=None,
                    help=f"look back this many days (default: since the last check, or {FIRST_RUN_DAYS} the first time)")
    ap.add_argument("--limit", type=int, default=0, help="at most this many emails")
    ap.add_argument("--again", action="store_true", help="reprocess emails already seen")
    args = ap.parse_args()

    summary = run_ingest(settings, args.days, args.limit, args.again, say=lambda m: print(m, flush=True))
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(summary["counts"].items())))
    print_spending(summary["transactions"], settings)
    print("\nReview it: python -m doraemon.web")


def print_spending(transactions, settings: Settings) -> None:
    ledger, duplicates = build_ledger(transactions)
    if not ledger:
        return
    home = settings.home_currency
    fx = FxRates(settings.db_path) if settings.convert_currencies else None
    sums = spending_totals(ledger, home, fx)
    print(f"Spending from emailed receipts and alerts ({len(duplicates)} duplicate receipts dropped):")
    for category, total in sorted(sums.by_category.items(), key=lambda kv: -kv[1]):
        print(f"   {category:14} {home} {total}")
    print(f"   {'total':14} {home} {sums.total}")
    converted = [t for t in ledger if t.currency != home and id(t) in sums.home_amounts]
    for txn in converted:
        print(f"     incl. {txn.currency} {txn.amount} {txn.merchant} = {home} {sums.home_amounts[id(txn)]}")
    if sums.unconverted:
        print(f"   Not in the total ({len(sums.unconverted)} with no exchange rate):")
        for txn in sums.unconverted:
            print(f"     {txn.currency} {txn.amount}  {txn.merchant}  ({txn.category.value})")
    print("   Only payments with an email are counted; cash and other card payments are missing.")


if __name__ == "__main__":
    main()
