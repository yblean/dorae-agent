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


def test_a_cancellation_and_the_banks_refund_count_once():
    # Trip.com's cancellation email names the hotel and says S$282.68; MariBank refunds S$278.05 from Trip.com
    cancelled = txn("r", RECEIPT, "Tiger Leaping Gorge yachajiao Manor", "282.68", 16, refund=True)
    refund = txn("b", BANK, "Trip.com", "278.05", 17, refund=True)
    kept, dups = build_ledger([cancelled, refund])
    assert kept == [refund] and dups[0].dropped is cancelled


def test_a_refund_arriving_days_later_still_matches_but_a_different_amount_doesnt():
    cancelled = txn("r", RECEIPT, "Klook", "120.00", 2, refund=True)
    assert build_ledger([cancelled, txn("b", BANK, "KLOOK TRAVEL", "120.00", 13, refund=True)])[0] != [cancelled]
    other = txn("b", BANK, "Shopee", "45.00", 3, refund=True)
    assert len(build_ledger([cancelled, other])[0]) == 2
    payment = txn("p", BANK, "Klook", "120.00", 3)  # a payment isn't a refund
    assert len(build_ledger([cancelled, payment])[0]) == 2


# --- refunds count in the month of the purchase they reverse -------------------------------

def test_a_refund_counts_on_the_day_of_its_purchase():
    from doraemon.ledger import counted_between
    from datetime import date
    booked = txn("b1", BANK, "Trip.com", "278.05", 22)
    refund = txn("b2", BANK, "Trip.com", "278.05", 22).model_copy(
        update={"purchased_at": datetime(2026, 10, 17, 13, tzinfo=SGT), "is_refund": True})
    ride = txn("b3", BANK, "Grab", "12.00", 23).model_copy(update={"purchased_at": datetime(2026, 10, 2, 9, tzinfo=SGT)})
    sep = counted_between([booked, refund, ride], date(2026, 9, 1), date(2026, 9, 30), SGT)
    oct_ = counted_between([booked, refund, ride], date(2026, 10, 1), date(2026, 10, 31), SGT)
    assert sep == [booked, refund] and oct_ == [ride]


def test_a_refund_without_its_purchase_counts_when_it_came():
    from doraemon.ledger import refunded_purchases
    other_shop = txn("b1", BANK, "Shopee", "40.00", 5)
    refund = txn("b2", BANK, "Lazada", "40.00", 9, refund=True)
    too_small = txn("b3", BANK, "Lazada", "10.00", 1)
    assert refunded_purchases([other_shop, refund, too_small]) == {}


def test_one_purchase_cant_take_two_full_refunds():
    from doraemon.ledger import refunded_purchases
    bought = txn("b1", BANK, "Uniqlo", "59.90", 3)
    r1, r2 = txn("r1", BANK, "UNIQLO SG", "59.90", 8, refund=True), txn("r2", BANK, "Uniqlo", "59.90", 9, refund=True)
    found = refunded_purchases([bought, r1, r2])
    assert found == {id(r1): bought}


def test_a_refund_told_twice_counts_once():
    # BookMarket's refund email and Trust's refund alert, both read as bank alerts; the bank writes B00KMARKET
    bought = txn("b1", BANK, "SP B00KMARKET +6597991669 SG", "13.50", 28)
    shop_says = txn("b2", BANK, "BookMarket", "13.50", 30, refund=True)
    bank_says = txn("b3", BANK, "SP B00KMARKET SINGAPORE", "13.50", 30, refund=True).model_copy(
        update={"purchased_at": datetime(2026, 10, 4, 23, tzinfo=SGT)})
    kept, dups = build_ledger([bought, shop_says, bank_says])
    assert kept == [bought, shop_says] and dups[0].dropped is bank_says


def test_two_real_refunds_of_one_order_both_count():
    from doraemon.ledger import refunded_purchases
    order = txn("b1", BANK, "Uniqlo", "100.00", 1)
    first, second = txn("r1", BANK, "Uniqlo", "30.00", 5, refund=True), txn("r2", BANK, "Uniqlo", "30.00", 8, refund=True)
    kept, _ = build_ledger([order, first, second])
    assert kept == [order, first, second] and len(refunded_purchases(kept)) == 2  # 60 of 100 back
