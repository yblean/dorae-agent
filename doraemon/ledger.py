"""Spending ledger: merge transactions from all emails without double counting.

Bank alerts are the source of truth, since they record every card payment.
A merchant receipt only counts when no bank alert covers the same payment,
e.g. Shopee paid from an account that sends no alerts.
"""
import re
from dataclasses import dataclass
from decimal import Decimal

from doraemon.schema import Category, Transaction, TxnSource

WINDOW_DAYS = 3
# Points, coins and vouchers can make the card charge smaller than the receipt total.
NAMED_MATCH_TOLERANCE = Decimal("0.05")
_NOISE = {"singapore", "pte", "ltd", "com", "the", "app", "store", "sg"}


def _name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("'", ""))
    return {w for w in words if len(w) >= 3 and w not in _NOISE}


def _close(a: Transaction, b: Transaction) -> bool:
    return (
        a.is_refund == b.is_refund
        and a.currency == b.currency
        and abs((a.purchased_at.date() - b.purchased_at.date()).days) <= WINDOW_DAYS
    )


def _names_match(a: Transaction, b: Transaction) -> bool:
    return bool(_name_tokens(a.merchant) & _name_tokens(b.merchant))


def same_payment(receipt: Transaction, other: Transaction) -> bool:
    """Is `receipt` (a merchant receipt) the same payment as `other`?"""
    # Same order number means same order, even when a forward arrives days later.
    if receipt.order_ref and receipt.order_ref == other.order_ref:
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
    return sorted(kept, key=lambda t: t.purchased_at), duplicates
