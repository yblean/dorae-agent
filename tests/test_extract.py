import json
from decimal import Decimal

from conftest import FakeBackend
from doraemon.extract import extract
from doraemon.schema import ItemType


def _item(**overrides):
    item = {
        "type": "bill", "title": "Pay electricity bill", "departs_from": None, "when": "Friday, October 16, 2026",
        "end": None, "timezone": None, "location": None, "booking_ref": None, "amount": 84.2, "currency": "usd",
        "evidence": "Your bill of $84.20 is due. Paid with card 4111 1111 1111 1111.", "confidence": 0.9,
    }
    return item | overrides


def test_extracts_and_resolves_bill(bill_email, settings):
    backend = FakeBackend(json.dumps({"items": [_item()], "transactions": []}))
    result = extract(bill_email, backend, settings)

    assert result.problems == []
    assert result.actionable
    [item] = result.items
    assert item.type == ItemType.BILL
    assert item.start_at.date().isoformat() == "2026-10-16"
    assert item.all_day
    assert item.amount == Decimal("84.20")
    assert item.currency == "USD"
    assert "4111" not in item.evidence_snippet
    assert "<email>" in backend.calls[0]


def test_invalid_model_output_is_reported_not_raised(bill_email, settings):
    result = extract(bill_email, FakeBackend('{"items": "nope"}'), settings)
    assert result.items == []
    assert result.problems


def test_unparseable_date_lowers_confidence(bill_email, settings):
    backend = FakeBackend(json.dumps({"items": [_item(when="soonish")], "transactions": []}))
    [item] = extract(bill_email, backend, settings).items
    assert item.start_at is None
    assert item.confidence <= 0.3


def test_flight_time_is_local_to_departure_airport(bill_email, settings):
    narita = _item(type="flight", when="2026/04/24 (Fri) 16:50", timezone="Asia/Tokyo")
    changi = _item(type="flight", when="2026/04/10 (Fri) 00:30", timezone="Asia/Singapore")
    backend = FakeBackend(json.dumps({"items": [narita, changi], "transactions": []}))
    back, out = extract(bill_email, backend, settings).items
    assert back.start_at.isoformat() == "2026-04-24T16:50:00+09:00"
    assert back.timezone == "Asia/Tokyo"
    assert out.start_at.isoformat() == "2026-04-10T00:30:00+08:00"


def test_unknown_timezone_falls_back_to_user_timezone(bill_email, settings):
    flight = _item(type="flight", when="2026/04/24 (Fri) 16:50", timezone="Narita/Somewhere")
    result = extract(bill_email, FakeBackend(json.dumps({"items": [flight], "transactions": []})), settings)
    assert result.items[0].timezone == settings.timezone
    assert any("unknown timezone" in p for p in result.problems)


def test_currency_symbols_become_iso_codes(bill_email, settings):
    backend = FakeBackend(json.dumps({"items": [_item(currency="S$")], "transactions": []}))
    assert extract(bill_email, backend, settings).items[0].currency == "SGD"
    backend = FakeBackend(json.dumps({"items": [_item(currency="$")], "transactions": []}))
    assert extract(bill_email, backend, settings).items[0].currency == settings.home_currency


def test_transaction_defaults_to_email_date(bill_email, settings):
    txn = {"source": "merchant_receipt", "merchant": "Acme", "order_ref": "A1", "purchased": None, "amount": -12.5,
           "currency": None, "category": "shopping", "is_refund": True}
    backend = FakeBackend(json.dumps({"items": [], "transactions": [txn]}))
    [t] = extract(bill_email, backend, settings).transactions
    assert t.purchased_at == bill_email.sent_at
    assert t.amount == Decimal("12.50")
    assert t.amount_home == Decimal("12.50")
