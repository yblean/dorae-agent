from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from doraemon.rules import RuleStore, load_defaults, name_key
from doraemon.schema import ActionItem, Extraction, Transaction

SG = ZoneInfo("Asia/Singapore")


def txn(merchant, category="other", amount="5.00"):
    return Transaction(message_id="m", source="bank_alert", merchant=merchant, order_ref=None,
                       purchased_at=datetime(2026, 10, 1, tzinfo=SG), amount=Decimal(amount), currency="SGD",
                       amount_home=None, category=category, is_refund=False)


def flight(departs_from, tz):
    return ActionItem(message_id="m", type="flight", title="ZG053", start_at=datetime(2026, 4, 24, 16, 50, tzinfo=ZoneInfo(tz)),
                      end_at=None, all_day=False, timezone=tz, departs_from=departs_from,
                      evidence_snippet="", confidence=0.9)


@pytest.mark.parametrize("name, key", [
    ("McDonalds 930255 Singapore SG", "mcdonalds"),
    ("MCDONALD'S (JP) SINGAPORE SG", "mcdonalds jp"),
    ("Kopitiam Investment Pte LSINGAPORE SG", "kopitiam investment"),
    ("EzypaySGD*Anytime FitnessSingapore SG", "ezypaysgd anytime fitness"),
    ("7-ELEVEN -213 BUKITBAT Singapore SG", "eleven bukitbat"),
])
def test_name_key(name, key):
    assert name_key(name) == key


def test_defaults_load_and_are_valid():
    rules = load_defaults()
    assert any(r.match == "kopitiam" and r.value == "dining" for r in rules)
    assert any(r.match == "narita" and r.value == "Asia/Tokyo" for r in rules)


def test_correction_fixes_every_branch():
    store = RuleStore(use_defaults=False)
    store.add("merchant_category", "Kopitiam Investment Pte LSINGAPORE SG", "dining", created_from="0003")
    ex = store.apply(Extraction(message_id="m", transactions=[txn("Kopitiam Investment Pte LSINGAPORE SG", "groceries")]))
    assert ex.transactions[0].category == "dining"
    assert "0003" in ex.applied_rules[0]


def test_sender_name_backs_up_merchant():
    store = RuleStore()  # default: shopee -> shopping
    ex = store.apply(Extraction(message_id="m", transactions=[txn("dollarsaver", "groceries")]),
                     sender="Shopee <info@mail.shopee.sg>")
    assert ex.transactions[0].category == "shopping"
    # A rule on the merchant itself still wins over the sender
    store.add("merchant_category", "dollarsaver", "health")
    ex = store.apply(Extraction(message_id="m", transactions=[txn("dollarsaver", "groceries")]),
                     sender="Shopee <info@mail.shopee.sg>")
    assert ex.transactions[0].category == "health"


def test_user_rule_beats_default():
    store = RuleStore()  # default: anytime fitness -> memberships
    store.add("merchant_category", "anytime fitness", "health")
    ex = store.apply(Extraction(message_id="m", transactions=[txn("EzypaySGD*Anytime FitnessSingapore SG")]))
    assert ex.transactions[0].category == "health"


def test_newer_correction_replaces_older():
    store = RuleStore(use_defaults=False)
    store.add("merchant_category", "Vivifi", "utilities")
    store.add("merchant_category", "VIVIFI", "subscriptions")
    assert [(r.match, r.value) for r in store.user_rules()] == [("vivifi", "subscriptions")]


def test_not_spending_drops_transaction():
    store = RuleStore(use_defaults=False)
    store.add("not_spending", "LEAN MUN SOON")
    ex = store.apply(Extraction(message_id="m", transactions=[txn("LEAN MUN SOON"), txn("NTUC FP")]))
    assert [t.merchant for t in ex.transactions] == ["NTUC FP"]


def test_travel_timezone_keeps_clock_changes_zone():
    store = RuleStore()  # default: changi -> Asia/Singapore
    ex = store.apply(Extraction(message_id="m", items=[flight("Changi Airport", "Asia/Tokyo")]))
    item = ex.items[0]
    assert item.start_at.isoformat() == "2026-04-24T16:50:00+08:00"
    assert item.timezone == "Asia/Singapore"


def test_ignore_sender_exact_address_and_domain():
    store = RuleStore(use_defaults=False)
    store.add("ignore_sender", "merewards <delights@enews.merewards.sg>")
    store.add("ignore_sender", "newsletter.shopee.sg")
    assert store.ignored_by("x <delights@enews.merewards.sg>")
    assert store.ignored_by("Shopee <info@newsletter.shopee.sg>")
    assert not store.ignored_by("Shopee <info@mail.shopee.sg>")  # receipts still come through


def test_rules_persist_in_sqlite(tmp_path):
    db = tmp_path / "doraemon.db"
    RuleStore(db).add("merchant_category", "Kopitiam", "dining", created_from="0003")
    assert [str(r) for r in RuleStore(db).user_rules()] == ["merchant_category: 'kopitiam' -> 'dining' (#1 from 0003)"]


def test_bad_rules_are_rejected():
    store = RuleStore(use_defaults=False)
    with pytest.raises(ValueError):
        store.add("merchant_category", "Kopitiam", "food")
    with pytest.raises(ValueError):
        store.add("travel_timezone", "Narita", "Japan/Tokyo")
    with pytest.raises(ValueError):
        store.add("merchant_category", "1234 SG", "dining")  # nothing left to match on
