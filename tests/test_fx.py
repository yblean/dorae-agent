from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from doraemon.fx import FxRates
from doraemon.ledger import spending_totals
from doraemon.schema import Transaction


def txn(merchant, amount, currency, category="dining", day=14, refund=False):
    return Transaction(message_id="m", source="bank_alert", merchant=merchant, order_ref=None,
                       purchased_at=datetime(2026, 9, day, 12, tzinfo=ZoneInfo("Asia/Singapore")),
                       amount=Decimal(amount), currency=currency, amount_home=None,
                       category=category, is_refund=refund)


class FakeRates:
    """Stands in for the Frankfurter API: same rate every business day, and counts calls."""

    def __init__(self, rates):
        self.rates, self.calls = rates, []

    def __call__(self, base, quote, start, end):
        self.calls.append((base, quote, start, end))
        if base not in self.rates:
            return {}
        days = (start + timedelta(days=n) for n in range((end - start).days + 1))
        return {d: self.rates[base] for d in days if d.weekday() < 5}  # no weekend rates, like the ECB


def test_converts_and_caches_a_range_in_one_call(tmp_path):
    fetch = FakeRates({"MYR": Decimal("0.31171")})
    fx = FxRates(tmp_path / "d.db", fetch)
    assert fx.to_home(txn("BOOST JUICE", "15.90", "MYR", day=14), "SGD") == Decimal("4.96")
    assert fx.to_home(txn("BOOST JUICE", "15.90", "MYR", day=16), "SGD") == Decimal("4.96")
    assert len(fetch.calls) == 1  # the second date came from the same fetched range
    # cached in the database: a page that never goes online can still convert
    assert FxRates(tmp_path / "d.db", fetch=None).to_home(txn("X", "15.90", "MYR", day=15), "SGD") == Decimal("4.96")


def test_weekend_purchase_uses_last_business_day_rate():
    fx = FxRates(fetch=FakeRates({"USD": Decimal("1.28")}))
    saturday = txn("Shop", "10", "USD", day=19)
    assert date(2026, 9, 19).weekday() == 5
    assert fx.to_home(saturday, "SGD") == Decimal("12.80")


def test_totals_include_converted_payments():
    fx = FxRates(fetch=FakeRates({"USD": Decimal("1.28"), "MYR": Decimal("0.31171")}))
    kept = [txn("Kopitiam", "7.00", "SGD"), txn("BOOST JUICE", "15.90", "MYR"),
            txn("Sleepysol", "20.00", "USD", category="subscriptions")]
    sums = spending_totals(kept, "SGD", fx)
    assert sums.by_category == {"dining": Decimal("11.96"), "subscriptions": Decimal("25.60")}
    assert sums.total == Decimal("37.56")
    assert sums.unconverted == []


def test_no_rate_keeps_payment_out_of_total():
    fx = FxRates(fetch=FakeRates({}))  # offline or unsupported currency
    sums = spending_totals([txn("Kopitiam", "7.00", "SGD"), txn("Somewhere", "500", "XYZ")], "SGD", fx)
    assert sums.total == Decimal("7.00")
    assert [t.currency for t in sums.unconverted] == ["XYZ"]


def test_refunds_are_subtracted_after_conversion():
    fx = FxRates(fetch=FakeRates({"USD": Decimal("1.28")}))
    sums = spending_totals([txn("Shop", "10", "USD", category="shopping", refund=True)], "SGD", fx)
    assert sums.by_category == {"shopping": Decimal("-12.80")}


def test_conversion_can_be_turned_off():
    sums = spending_totals([txn("Shop", "10", "USD")], "SGD", fx=None)
    assert sums.total == 0 and len(sums.unconverted) == 1
