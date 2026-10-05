from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon.ledger import build_ledger
from doraemon.schema import Transaction, TxnSource

SGT = ZoneInfo("Asia/Singapore")
BANK, RECEIPT = TxnSource.BANK_ALERT, TxnSource.MERCHANT_RECEIPT


def txn(msg, source, merchant, amount, day, refund=False):
    return Transaction(
        message_id=msg, source=source, merchant=merchant, order_ref=None,
        purchased_at=datetime(2026, 9, day, 12, tzinfo=SGT), amount=Decimal(amount),
        currency="SGD", amount_home=None, category="other", is_refund=refund,
    )


def test_receipt_matching_bank_amount_is_dropped():
    bank = txn("b", BANK, "McDonalds 930255", "4.70", 2)
    receipt = txn("r", RECEIPT, "McDonald's", "4.70", 2)
    kept, dups = build_ledger([bank, receipt])
    assert kept == [bank]
    assert dups[0].dropped is receipt


def test_receipt_without_bank_alert_is_kept():
    receipt = txn("r", RECEIPT, "Shopee", "8.61", 3)
    kept, _ = build_ledger([txn("b", BANK, "BUS/MRT", "1.28", 3), receipt])
    assert receipt in kept


def test_same_merchant_small_difference_is_dropped():
    # Trip.com total S$282.68, card charged S$278.05 after Trip Coins
    bank = txn("b", BANK, "Trip.com", "278.05", 22)
    kept, dups = build_ledger([bank, txn("r", RECEIPT, "Trip.com", "282.68", 22)])
    assert kept == [bank] and len(dups) == 1


def test_different_merchant_small_difference_is_kept():
    kept, _ = build_ledger([txn("b", BANK, "Kopitiam", "7.02", 22), txn("r", RECEIPT, "Shopee", "7.10", 22)])
    assert len(kept) == 2


def test_far_apart_dates_are_kept():
    kept, _ = build_ledger([txn("b", BANK, "X", "4.70", 1), txn("r", RECEIPT, "Y", "4.70", 10)])
    assert len(kept) == 2


def test_forwarded_receipt_is_dropped_without_bank_alert():
    original = txn("r1", RECEIPT, "Shopee", "21.02", 5)
    kept, _ = build_ledger([original, txn("r2", RECEIPT, "Shopee", "21.02", 5)])
    assert kept == [original]


def test_forward_of_dropped_receipt_is_dropped_by_order_ref():
    bank = txn("b", BANK, "Trip.com", "278.05", 22)
    original = txn("r1", RECEIPT, "Trip.com", "282.68", 22).model_copy(update={"order_ref": "157895"})
    forward = txn("r2", RECEIPT, "Trip.com", "282.68", 29).model_copy(update={"order_ref": "157895"})
    kept, dups = build_ledger([bank, original, forward])
    assert kept == [bank] and len(dups) == 2


def test_kept_alert_takes_category_from_dropped_receipt():
    bank = txn("b", BANK, "ANTHROPIC* CLAUDE SUB +14152360599 US", "30.00", 3)
    receipt = txn("r", RECEIPT, "Anthropic, PBC", "30.00", 3).model_copy(update={"category": "subscriptions"})
    kept, _ = build_ledger([bank, receipt])
    assert kept == [bank] and bank.category == "subscriptions"


def test_specific_alert_category_is_kept():
    bank = txn("b", BANK, "McDonalds 930255", "4.70", 2).model_copy(update={"category": "dining"})
    receipt = txn("r", RECEIPT, "McDonald's", "4.70", 2).model_copy(update={"category": "shopping"})
    build_ledger([bank, receipt])
    assert bank.category == "dining"


def test_two_bank_alerts_same_amount_both_count():
    # Two bus rides at S$1.28 are two real payments
    kept, _ = build_ledger([txn("b1", BANK, "BUS/MRT", "1.28", 1), txn("b2", BANK, "BUS/MRT", "1.28", 1)])
    assert len(kept) == 2
