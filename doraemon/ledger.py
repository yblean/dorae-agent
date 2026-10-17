"""Spending ledger: merge transactions from all emails without double counting.

Bank alerts are the source of truth, since they record every card payment.
A merchant receipt only counts when no bank alert covers the same payment,
e.g. Shopee paid from an account that sends no alerts. A refund counts in the
month of the purchase it reverses (counted_between), when that purchase is found.
"""
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon.schema import Category, Transaction, TxnSource

WINDOW_DAYS = 3
# A refund reaches the card days after the cancellation email, sometimes a couple of weeks.
REFUND_WINDOW_DAYS = 14
REFUND_LOOKBACK_DAYS = 90  # a refund counts in the month of a purchase up to this long before it
# Points, coins and vouchers can make the card charge smaller than the receipt total.
NAMED_MATCH_TOLERANCE = Decimal("0.05")
_NOISE = {"singapore", "pte", "ltd", "com", "the", "app", "store", "sg"}


def name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("'", ""))
    # Banks sometimes write O as zero inside a name ("SP B00KMARKET" for BookMarket)
    words = [w.replace("0", "o") if re.search(r"[a-z]", w) else w for w in words]
    return {w for w in words if len(w) >= 3 and w not in _NOISE}


def _close(a: Transaction, b: Transaction) -> bool:
    return (
        a.is_refund == b.is_refund
        and a.currency == b.currency
        and abs((a.purchased_at.date() - b.purchased_at.date()).days) <= WINDOW_DAYS
    )


def _names_match(a: Transaction, b: Transaction) -> bool:
    return bool(name_tokens(a.merchant) & name_tokens(b.merchant))


def _same_refund(receipt: Transaction, other: Transaction) -> bool:
    """A cancellation email's refund and the bank's refund alert for it. The names rarely match (the email names
    the hotel, the bank names Trip.com) and coins used on the booking make the amounts differ a little, but
    refunds are rare enough that a close amount within a couple of weeks is the same one."""
    if not (receipt.is_refund and other.is_refund and other.source == TxnSource.BANK_ALERT
            and receipt.currency == other.currency):
        return False
    if abs((receipt.purchased_at.date() - other.purchased_at.date()).days) > REFUND_WINDOW_DAYS:
        return False
    return abs(receipt.amount - other.amount) <= max(receipt.amount, other.amount) * NAMED_MATCH_TOLERANCE


def same_payment(receipt: Transaction, other: Transaction) -> bool:
    """Is `receipt` (a merchant receipt) the same payment as `other`?"""
    # Same order number means same order, even when a forward arrives days later.
    if receipt.order_ref and receipt.order_ref == other.order_ref:
        return True
    if _same_refund(receipt, other):
        return True
    if not _close(receipt, other):
        return False
    named = _names_match(receipt, other)
    if receipt.amount == other.amount:
        # Bank payee names are often useless ("STRIPE PAYMENTS"), so an exact
        # amount is enough against a bank alert. Two receipts also need the same merchant.
        return other.source == TxnSource.BANK_ALERT or named
    biggest = max(receipt.amount, other.amount)
    return named and abs(receipt.amount - other.amount) <= biggest * NAMED_MATCH_TOLERANCE


@dataclass
class Duplicate:
    dropped: Transaction
    kept: Transaction


def build_ledger(transactions: list[Transaction]) -> tuple[list[Transaction], list[Duplicate]]:
    kept = [t for t in transactions if t.source == TxnSource.BANK_ALERT]
    duplicates: list[Duplicate] = []
    receipts = sorted(
        (t for t in transactions if t.source == TxnSource.MERCHANT_RECEIPT),
        key=lambda t: t.purchased_at,
    )
    for receipt in receipts:
        # Also check receipts already dropped, so a forward of a receipt that matched
        # a bank alert is dropped too.
        seen = kept + [d.dropped for d in duplicates]
        match = next((k for k in seen if same_payment(receipt, k)), None)
        if match:
            duplicates.append(Duplicate(dropped=receipt, kept=match))
            # A bank payee like "ANTHROPIC* CLAUDE SUB +1415..." often lands in `other`;
            # the merchant's own receipt knows better.
            if match.category == Category.OTHER and receipt.category != Category.OTHER:
                match.category = receipt.category
        else:
            kept.append(receipt)
    for repeat in _match_refunds(kept)[1]:  # one refund told twice
        kept.remove(repeat.dropped)
        duplicates.append(repeat)
    return sorted(kept, key=lambda t: t.purchased_at), duplicates


def _match_refunds(kept: list[Transaction]) -> tuple[dict[int, Transaction], list[Duplicate]]:
    """(id(refund) -> the purchase it gives money back for, refunds that repeat one already counted).

    A refund's purchase is the latest earlier payment to the same merchant (by name), in the same currency,
    within REFUND_LOOKBACK_DAYS, with enough of it not yet refunded. A purchase can't be refunded more than
    it cost: a second refund of about the same amount within REFUND_WINDOW_DAYS of the first is the same
    refund told twice (the shop's email and the bank's alert), so it's a repeat. Refunds with no purchase
    (it was before your email starts, or the names differ) are in neither."""
    purchases = sorted((t for t in kept if not t.is_refund), key=lambda t: t.purchased_at)
    left = {id(p): p.amount for p in purchases}
    refunds_of: dict[int, list[Transaction]] = {id(p): [] for p in purchases}
    found: dict[int, Transaction] = {}
    repeats: list[Duplicate] = []
    for refund in sorted((t for t in kept if t.is_refund), key=lambda t: t.purchased_at):
        named = [p for p in purchases if p.currency == refund.currency and p.purchased_at <= refund.purchased_at
                 and (refund.purchased_at - p.purchased_at).days <= REFUND_LOOKBACK_DAYS and _names_match(refund, p)]
        open_ = [p for p in named if left[id(p)] >= refund.amount * (1 - NAMED_MATCH_TOLERANCE)]
        if open_:
            found[id(refund)] = open_[-1]
            left[id(open_[-1])] -= refund.amount
            refunds_of[id(open_[-1])].append(refund)
            continue
        earlier = next((r for p in reversed(named) for r in refunds_of[id(p)]
                        if abs(r.amount - refund.amount) <= max(r.amount, refund.amount) * NAMED_MATCH_TOLERANCE
                        and abs((refund.purchased_at - r.purchased_at).days) <= REFUND_WINDOW_DAYS), None)
        if earlier is not None:
            repeats.append(Duplicate(dropped=refund, kept=earlier))
    return found, repeats


def refunded_purchases(kept: list[Transaction]) -> dict[int, Transaction]:
    """id(refund) -> the purchase it gives money back for (see _match_refunds)."""
    return _match_refunds(kept)[0]


def counted_between(transactions: list[Transaction], first: date | None, last: date | None,
                    tz: ZoneInfo) -> list[Transaction]:
    """The ledger of the payments that count from `first` to `last` (days in `tz`, both included; None: no limit).
    A refund counts on the day of the purchase it gives money back for, so a cancelled September booking and its
    October refund cancel out in September instead of making October's spending look smaller."""
    kept, _ = build_ledger(transactions)
    originals = refunded_purchases(kept)
    day = lambda t: originals.get(id(t), t).purchased_at.astimezone(tz).date()
    return [t for t in kept if (first is None or day(t) >= first) and (last is None or day(t) <= last)]


@dataclass
class Totals:
    by_category: dict[str, Decimal]
    total: Decimal
    home_amounts: dict[int, Decimal]  # id(txn) -> amount in the home currency, for display
    unconverted: list[Transaction]    # foreign payments with no exchange rate available


def spending_totals(kept: list[Transaction], home: str, fx=None) -> Totals:
    """Sum a ledger by category in the home currency, converting foreign payments when `fx` is given."""
    by_category: dict[str, Decimal] = {}
    home_amounts: dict[int, Decimal] = {}
    unconverted = []
    for txn in kept:
        amount = txn.amount if txn.currency == home else (fx.to_home(txn, home) if fx else None)
        if amount is None:
            unconverted.append(txn)
            continue
        home_amounts[id(txn)] = amount
        signed = -amount if txn.is_refund else amount
        by_category[txn.category.value] = by_category.get(txn.category.value, Decimal(0)) + signed
    return Totals(by_category, sum(by_category.values(), Decimal(0)), home_amounts, unconverted)
