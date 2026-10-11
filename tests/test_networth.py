"""Net worth, income, brokerage sync (Webull SG) and the monthly check-in."""
import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon import income
from doraemon.brokers import BrokerError, sync
from doraemon.brokers.webull import WebullSG
from doraemon.db import Database
from doraemon.jobs import Runtime
from doraemon.networth import NetWorth, check_in_day
from doraemon.router import route
from doraemon.web import create_app

TODAY = date(2026, 10, 11)


class FakeFx:
    def rate(self, base, quote, day, online=True):
        return Decimal("1.30") if (base, quote) == ("USD", "SGD") else None


@pytest.fixture
def nw(tmp_path):
    return NetWorth(Database(tmp_path / "d.db"), "SGD", FakeFx())


# --- adding it up -------------------------------------------------------------------------------

def test_net_worth_adds_assets_and_takes_off_loans(nw):
    bank = nw.add_account("DBS", "cash", "SGD")
    usd = nw.add_account("Webull", "investment", "USD")
    loan = nw.add_account("Car loan", "loan", "SGD")
    nw.set_balance(bank, Decimal("10000"), date(2026, 9, 30))
    nw.set_balance(usd, Decimal("1000"), date(2026, 9, 30))  # = SGD 1300
    nw.set_balance(loan, Decimal("4000"), date(2026, 9, 30))
    w = nw.worth_at(TODAY)
    assert (w.assets, w.liabilities, w.total) == (Decimal("11300.00"), Decimal("4000.00"), Decimal("7300.00"))
    assert w.by_kind == {"cash": Decimal("10000.00"), "investment": Decimal("1300.00"), "loan": Decimal("4000.00")}


def test_each_day_uses_the_latest_balance_up_to_then(nw):
    bank = nw.add_account("DBS", "cash", "SGD")
    nw.set_balance(bank, Decimal("100"), date(2026, 8, 31))
    nw.set_balance(bank, Decimal("300"), date(2026, 9, 30))
    nw.set_balance(bank, Decimal("350"), date(2026, 9, 30))  # same day: replaced, not added
    assert nw.worth_at(date(2026, 9, 15)).total == Decimal("100.00")
    assert nw.worth_at(TODAY).total == Decimal("350.00")


def test_month_on_month_change_and_trend(nw):
    bank = nw.add_account("DBS", "cash", "SGD")
    nw.set_balance(bank, Decimal("1000"), date(2026, 8, 31))
    nw.set_balance(bank, Decimal("1500"), date(2026, 9, 30))
    nw.set_balance(bank, Decimal("1800"), date(2026, 10, 10))
    ch = nw.change(TODAY)
    assert (ch["change"], ch["pct"]) == (Decimal("300.00"), Decimal("20.0"))
    months = nw.months(12, TODAY)
    assert [(m["month"], m["total"], m["partial"]) for m in months] == [
        ("2026-08", Decimal("1000.00"), False), ("2026-09", Decimal("1500.00"), False), ("2026-10", Decimal("1800.00"), True)]


def test_no_exchange_rate_leaves_it_out_and_says_so(nw):
    acc = nw.add_account("Yen savings", "cash", "JPY")
    nw.set_balance(acc, Decimal("50000"), TODAY)
    w = nw.worth_at(TODAY)
    assert w.total == 0 and w.unconverted == ["Yen savings"]


def test_closing_an_account_keeps_its_past(nw):
    bank = nw.add_account("Old bank", "cash", "SGD")
    nw.set_balance(bank, Decimal("500"), date(2026, 9, 1))
    nw.archive(bank)
    assert nw.worth_at(date(2026, 9, 30)).total == Decimal("500.00")
    assert nw.worth_at(datetime.now(timezone.utc).date()).total == 0
    assert nw.accounts() == []


def test_balances_typed_in_the_first_week_count_for_last_month():
    assert check_in_day(date(2026, 11, 3)) == date(2026, 10, 31)
    assert check_in_day(date(2026, 11, 8)) == date(2026, 11, 8)


# --- income -----------------------------------------------------------------------------------------

def test_monthly_salary_and_one_offs(tmp_path):
    db = Database(tmp_path / "d.db")
    income.add(db, "Salary", "salary", Decimal("5000"), "SGD", date(2026, 8, 31), monthly=True)
    income.add(db, "Bonus", "bonus", Decimal("2000"), "SGD", date(2026, 9, 15))
    income.add(db, "US dividends", "dividends", Decimal("100"), "USD", date(2026, 9, 20))
    sept = income.in_month(db, "2026-09", "SGD", FakeFx(), TODAY)
    assert [(r.label, r.day.day, r.home) for r in sept] == [
        ("Bonus", 15, Decimal("2000")), ("US dividends", 20, Decimal("130.00")), ("Salary", 30, Decimal("5000"))]
    assert income.total(sept) == Decimal("7130.00")
    assert income.in_month(db, "2026-07", "SGD", FakeFx(), TODAY) == []  # before it started


def test_this_months_salary_counts_once_it_arrives_and_stops(tmp_path):
    db = Database(tmp_path / "d.db")
    salary = income.add(db, "Salary", "salary", Decimal("5000"), "SGD", date(2026, 1, 25), monthly=True)
    assert income.in_month(db, "2026-10", "SGD", today=TODAY) == []  # the 25th hasn't come yet
    assert len(income.in_month(db, "2026-10", "SGD", today=date(2026, 10, 26))) == 1
    income.stop(db, salary, date(2026, 10, 26))
    assert income.in_month(db, "2026-11", "SGD", today=date(2026, 11, 30)) == []


# --- brokerage sync ---------------------------------------------------------------------------------

class FakeBroker:
    source, label = "webull", "Webull"

    def __init__(self, value="1000", fail=None):
        self.value, self.fail = value, fail

    def accounts(self):
        if self.fail:
            raise BrokerError(self.fail)
        return [{"id": "A1", "name": "Margin", "currency": "USD"}]

    def snapshot(self, account_id):
        return {"value": Decimal(self.value), "currency": "USD", "holdings": [
            {"symbol": "VOO", "name": "Vanguard S&P 500", "quantity": Decimal("2"), "price": Decimal("450"),
             "market_value": Decimal("900"), "cost": Decimal("800"), "currency": "USD"}]}


def test_sync_makes_one_investment_account_and_updates_it(nw):
    assert sync(nw, FakeBroker(), TODAY) == ["Webull: USD 1,000.00 (1 position)"]
    sync(nw, FakeBroker("1100"), TODAY)
    [acc] = nw.accounts()
    assert (acc["name"], acc["kind"], acc["source"], acc["external_id"]) == ("Webull Margin", "investment", "webull", "A1")
    assert nw.worth_at(TODAY).total == Decimal("1430.00")  # 1100 USD at 1.30
    assert [h["symbol"] for h in nw.holdings(acc["id"])] == ["VOO"]


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class FakeSdk:
    """Shaped like webull-openapi-python-sdk's TradeClient, with the names it uses."""

    class account_v2:
        @staticmethod
        def get_account_list():
            return FakeResponse({"data": [{"account_id": "123", "account_label": "Cash", "currency": "USD"}]})

        @staticmethod
        def get_account_balance(account_id):
            return FakeResponse({"total_asset_currency": "USD", "account_currency_assets": [
                {"currency": "USD", "total_asset": "1520.50", "cash_balance": "120.50"}]})

        @staticmethod
        def get_account_position(account_id):
            return FakeResponse({"holdings": [{"symbol": "AAPL", "instrument_name": "Apple", "qty": "5",
                                               "last_price": "280", "unit_cost": "250"}]})


def test_webull_adapter_reads_accounts_value_and_positions():
    broker = WebullSG("key", "secret", client=FakeSdk())
    assert broker.accounts() == [{"id": "123", "name": "Cash", "currency": "USD"}]
    snap = broker.snapshot("123")
    assert (snap["value"], snap["currency"]) == (Decimal("1520.50"), "USD")
    [h] = snap["holdings"]
    assert (h["symbol"], h["quantity"], h["market_value"], h["cost"]) == ("AAPL", Decimal("5"), Decimal("1400"), Decimal("1250"))


def test_webull_errors_never_carry_request_details():
    class Broken:
        class account_v2:
            @staticmethod
            def get_account_list():
                raise RuntimeError("POST https://api.webull.com.sg/... app_key=SECRET")
    with pytest.raises(BrokerError) as e:
        WebullSG("k", "s", client=Broken()).accounts()
    assert "SECRET" not in str(e.value) and "RuntimeError" in str(e.value)
    with pytest.raises(BrokerError, match="HTTP 401"):
        WebullSG("k", "s", client=type("C", (), {"account_v2": type("A", (), {
            "get_account_list": staticmethod(lambda: FakeResponse({}, 401))})})()).accounts()


# --- in the app ---------------------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path, settings):
    return dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), home_currency="SGD", timezone="Asia/Singapore",
                               google_token=str(tmp_path / "t.json"), google_credentials=str(tmp_path / "c.json"))


def test_broker_problems_are_said_once(settings):
    rt = Runtime(settings, brokers=[FakeBroker(fail="Webull said no (HTTP 401)")])
    rt.sync_brokers()
    rt.sync_brokers()
    said = [m["text"] for m in rt.db.messages("money")]
    assert said == ["I couldn't update your Webull account: Webull said no (HTTP 401)."]


class FakeBot:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)


def test_check_in_on_the_first_once(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    rt.brain.nw.set_balance(rt.brain.nw.add_account("DBS", "cash", "SGD"), Decimal("1000"), date(2026, 9, 30))
    tz = ZoneInfo("Asia/Singapore")
    assert not rt.check_in_tick(datetime(2026, 11, 1, 7, 0, tzinfo=tz))   # before the briefing time
    assert rt.check_in_tick(datetime(2026, 11, 1, 8, 0, tzinfo=tz))
    assert not rt.check_in_tick(datetime(2026, 11, 1, 9, 0, tzinfo=tz))   # once a month
    assert not rt.check_in_tick(datetime(2026, 11, 2, 9, 0, tzinfo=tz))
    text = rt.db.last_message("money")["text"]
    assert text.startswith("📊 It's the 1st: time for your monthly check-in.") and "end of October" in text
    assert bot.sent and "Monthly check-in" in bot.sent[0]


@pytest.mark.parametrize("question, intent", [
    ("what's my net worth", "networth"), ("how did my net worth change this month", "networth"),
    ("how much did I earn last month", "income"), ("how much did i save this month", "income"),
    ("my investments", "holdings")])
def test_questions_take_the_fast_path(question, intent):
    assert route("money", question).intent == intent
    assert route("money", "how can I grow my net worth faster") is None


def test_pages_forms_and_chat_answers(settings):
    client = TestClient(create_app(settings), follow_redirects=True)
    assert "No accounts yet" in client.get("/networth").text and "Connect Webull SG" in client.get("/networth").text
    client.post("/networth/accounts", data={"name": "DBS Multiplier", "kind": "cash", "currency": "sgd", "balance": "12,000"})
    client.post("/networth/accounts", data={"name": "HDB loan", "kind": "loan", "currency": "SGD", "balance": "2000"})
    today = datetime.now(ZoneInfo("Asia/Singapore")).date()  # newer than the balances the accounts started with
    page = client.post("/networth/balances", data={"as_of": today.isoformat(), "value:1": "13500", "value:2": ""}).text
    assert f"Saved 1 balance as of {today:%a} {today.day} {today:%b}" in page and "SGD 11,500.00" in page
    assert "isn&#39;t a currency code" in client.post("/networth/accounts", data={"name": "X", "kind": "cash", "currency": "dollars"}).text
    page = client.post("/income", data={"label": "Salary", "category": "salary", "amount": "5,000", "currency": "",
                                        "day": "2026-10-01", "monthly": "1"}).text
    assert "Added Salary: SGD 5,000.00 every month from 01 Oct." in page
    assert "Income and spending" in client.get("/income").text
    client.headers["x-requested-with"] = "fetch"
    html = client.post("/chat/money/ask", data={"q": "what's my net worth?"}).json()["html"]
    assert "Your net worth is SGD 11,500.00" in html and "Bank &amp; cash" in html and 'class="instant"' in html
    html = client.post("/chat/money/ask", data={"q": "how much did I earn this month"}).json()["html"]
    assert "You received SGD 5,000.00" in html and "Kept" in html


def test_closing_and_deleting(settings):
    client = TestClient(create_app(settings), follow_redirects=True)
    client.post("/networth/accounts", data={"name": "Old bank", "kind": "cash", "balance": "10"})
    assert "Closed Old bank" in client.post("/networth/accounts/1/archive").text
    client.post("/income", data={"label": "Gig", "category": "freelance", "amount": "50", "day": "2026-10-02"})
    assert "Deleted" in client.post("/income/1/delete").text
