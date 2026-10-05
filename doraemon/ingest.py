"""Milestone 4: pull recent Gmail, run the pipeline, print what it found.

    python -m doraemon.ingest                 # last 30 days, emails not seen before
    python -m doraemon.ingest --days 7 --limit 20
    python -m doraemon.ingest --again         # reprocess emails already seen

Read-only: nothing is written to Gmail or your calendar yet. Results are appended
to data/ingest.jsonl until the review page (milestone 6) takes over.
"""
import argparse
import dataclasses
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import httpx
from googleapiclient.errors import HttpError

from doraemon.config import Settings
from doraemon.db import Database
from doraemon.display import describe_when
from doraemon.email_parse import gmail_categories, parse_eml
from doraemon.gmail import GmailMessage, connect, fetch, list_ids
from doraemon.ledger import build_ledger
from doraemon.llm import Backend, get_backend
from doraemon.pipeline import process
from doraemon.rules import RuleStore
from doraemon.schema import Extraction

RESULTS_PATH = Path("data/ingest.jsonl")


def ingest_message(msg: GmailMessage, backend: Backend, settings: Settings, rules: RuleStore) -> tuple[str, Extraction]:
    email = parse_eml(msg.raw)
    # Gmail's own labels are more reliable than the Takeout header the parser reads
    email = dataclasses.replace(email, gmail_categories=gmail_categories(msg.label_ids))
    result = process(email, backend, settings, rules)
    return email.subject, result


def triage_label(result: Extraction) -> str:
    if result.skipped:
        return f"skipped: {result.skipped}"
    return "extracted" if (result.items or result.transactions) else "nothing found"


def print_result(subject: str, result: Extraction, settings: Settings) -> None:
    print(f"\n✉  {subject[:90]}")
    for item in result.items:
        amount = f"  {item.currency} {item.amount}" if item.amount else ""
        print(f"   [{item.type.value}] {item.title} | {describe_when(item, settings.timezone)}{amount}")
    for txn in result.transactions:
        refund = " (refund)" if txn.is_refund else ""
        print(f"   [spend] {txn.currency} {txn.amount}{refund}  {txn.merchant}  ({txn.category.value})")
    for note in result.applied_rules + result.problems:
        print(f"   · {note}")


def main() -> None:
    settings = Settings()
    ap = argparse.ArgumentParser(prog="python -m doraemon.ingest")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0, help="at most this many emails")
    ap.add_argument("--again", action="store_true", help="reprocess emails already seen")
    args = ap.parse_args()

    service = connect(settings.google_credentials, settings.google_token)
    db = Database(settings.db_path)
    rules = RuleStore(settings.db_path)
    backend = get_backend(settings.model, settings)

    ids = list_ids(service, f"newer_than:{args.days}d -in:chats", args.limit)
    todo = [i for i in reversed(ids) if args.again or not db.is_processed(i)]  # oldest first
    print(f"{len(ids)} emails in the last {args.days} days, {len(todo)} to process with {backend.name}")

    counts: dict[str, int] = defaultdict(int)
    transactions = []
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a", encoding="utf-8") as out:
        for n, message_id in enumerate(todo, 1):
            try:
                msg = fetch(service, message_id)
                subject, result = ingest_message(msg, backend, settings, rules)
            except (HttpError, httpx.HTTPError) as e:
                print(f"\n[{n}/{len(todo)}] failed, will retry next run: {e}")
                counts["failed"] += 1
                continue
            label = triage_label(result)
            counts[label.split(":")[0]] += 1
            db.mark_processed(msg.id, msg.thread_id, label)
            out.write(json.dumps({"gmail_id": msg.id, "subject": subject,
                                  "result": result.model_dump(mode="json")}) + "\n")
            out.flush()
            transactions += result.transactions
            if result.items or result.transactions:
                print_result(subject, result, settings)
            else:
                print(f"[{n}/{len(todo)}] {label}: {subject[:70]}", flush=True)

    ledger, duplicates = build_ledger(transactions)
    by_category: dict[str, Decimal] = defaultdict(Decimal)
    for txn in ledger:
        by_category[txn.category.value] += -txn.amount if txn.is_refund else txn.amount
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if ledger:
        print(f"Spending from emailed receipts and alerts ({len(duplicates)} duplicate receipts dropped):")
        for category, total in sorted(by_category.items(), key=lambda kv: -kv[1]):
            print(f"   {category:14} {settings.home_currency} {total}")
        print("   Only payments with an email are counted; cash and other card payments are missing.")


if __name__ == "__main__":
    main()
