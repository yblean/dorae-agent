"""Net worth: your accounts, their balances over time, and how the total changed month on month.

Each balance is kept with the day it was true, so net worth on any day is every account's latest
balance up to that day, converted to your home currency at that day's rate. Month-end values give
the month-on-month (MoM) change and the trend chart; nothing extra has to be saved each month.

Banks have no open API, so you enter cash, CPF, property and loans yourself (a monthly check-in on
the 1st). Brokerage accounts (doraemon.brokers) update themselves. Loans count against you.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from doraemon.db import Database

KINDS = {"cash": "Bank & cash", "cpf": "CPF", "investment": "Investments", "property": "Property",
         "other": "Other assets", "loan": "Loans & debts"}
LIABILITIES = {"loan"}
CHECK_IN_DAYS = 7  # balances entered in the first week of a month count for the month that just ended


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), ROUND_HALF_UP)


def month_end(month: str) -> date:
    first = date.fromisoformat(month + "-01")
    return (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def shift_month(month: str, n: int) -> str:
    y, m = divmod(int(month[:4]) * 12 + int(month[5:]) - 1 + n, 12)
    return f"{y}-{m + 1:02d}"


def check_in_day(today: date) -> date:
    """The day a balance typed today counts for: the last day of last month during the first week."""
    return today.replace(day=1) - timedelta(days=1) if today.day <= CHECK_IN_DAYS else today


@dataclass
class Worth:
    day: date
    total: Decimal = Decimal(0)
    assets: Decimal = Decimal(0)
    liabilities: Decimal = Decimal(0)
    by_kind: dict[str, Decimal] = field(default_factory=dict)
    accounts: list[dict] = field(default_factory=list)   # each with its balance, as_of and home value
    unconverted: list[str] = field(default_factory=list)  # accounts left out: no exchange rate

    @property
    def counted(self) -> bool:
        return any(a["value"] is not None for a in self.accounts)


class NetWorth:
    def __init__(self, db: Database, home: str, fx=None) -> None:
        self.db, self.home, self.fx = db, home, fx

    # --- accounts and balances -------------------------------------------------------------

    def accounts(self, archived: bool = False):
        return self.db.conn.execute("SELECT * FROM accounts WHERE archived = ? ORDER BY id", (int(archived),)).fetchall()

    def account(self, account_id: int):
        return self.db.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def add_account(self, name: str, kind: str, currency: str, source: str = "manual",
                    external_id: str | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        cur = self.db.conn.execute(
            "INSERT INTO accounts (name, kind, currency, source, external_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name.strip()[:60], kind, currency.upper(), source, external_id, _now()))
        self.db.conn.commit()
        return cur.lastrowid

    def find(self, source: str, external_id: str):
        return self.db.conn.execute("SELECT * FROM accounts WHERE source = ? AND external_id = ?",
                                    (source, external_id)).fetchone()

    def archive(self, account_id: int, archived: bool = True, today: date | None = None) -> None:
        """Hide an account you closed; its past balances still count for the months it existed."""
        if archived:  # from now on it's worth nothing
            self.set_balance(account_id, Decimal(0), today or datetime.now(timezone.utc).date(), "manual")
        self.db.conn.execute("UPDATE accounts SET archived = ? WHERE id = ?", (int(archived), account_id))
        self.db.conn.commit()

    def set_balance(self, account_id: int, value: Decimal, as_of: date, source: str = "manual") -> None:
        self.db.conn.execute(
            """INSERT INTO balances (account_id, as_of, value, source, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (account_id, as_of) DO UPDATE SET value = excluded.value, source = excluded.source,
               updated_at = excluded.updated_at""",
            (account_id, as_of.isoformat(), str(cents(Decimal(value))), source, _now()))
        self.db.conn.commit()

    def balance_at(self, account_id: int, day: date):
        """The latest balance on or before `day`, or None."""
        return self.db.conn.execute(
            "SELECT * FROM balances WHERE account_id = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
            (account_id, day.isoformat())).fetchone()

    def history(self, account_id: int, limit: int = 12):
        return self.db.conn.execute("SELECT * FROM balances WHERE account_id = ? ORDER BY as_of DESC LIMIT ?",
                                    (account_id, limit)).fetchall()

    def first_day(self) -> date | None:
        row = self.db.conn.execute("SELECT MIN(as_of) FROM balances").fetchone()
        return date.fromisoformat(row[0]) if row[0] else None

    # --- what it adds up to ----------------------------------------------------------------------

    def to_home(self, value: Decimal, currency: str, day: date) -> Decimal | None:
        if currency == self.home:
            return value
        rate = self.fx.rate(currency, self.home, day, online=False) if self.fx else None
        return cents(value * rate) if rate is not None else None

    def worth_at(self, day: date) -> Worth:
        """Net worth on `day`: every account's latest balance up to then, in your home currency."""
        w = Worth(day)
        rows = self.db.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        for a in rows:
            b = self.balance_at(a["id"], day)
            if b is None:
                if not a["archived"]:
                    w.accounts.append({"row": a, "value": None, "home": None, "as_of": None})
                continue
            value = Decimal(b["value"])
            home = self.to_home(value, a["currency"], date.fromisoformat(b["as_of"]))
            if not a["archived"] or value:
                w.accounts.append({"row": a, "value": value, "home": home, "as_of": b["as_of"], "source": b["source"]})
            if home is None:
                w.unconverted.append(a["name"])
                continue
            if a["kind"] in LIABILITIES:
                w.liabilities += home
            else:
                w.assets += home
            w.by_kind[a["kind"]] = w.by_kind.get(a["kind"], Decimal(0)) + home
        w.total = w.assets - w.liabilities
        return w

    def months(self, n: int = 12, today: date | None = None) -> list[dict]:
        """Net worth at each month's end for the last `n` months (this month: today), oldest first,
        starting from the first month that has a balance."""
        today = today or datetime.now(timezone.utc).date()
        first = self.first_day()
        if first is None:
            return []
        this = today.strftime("%Y-%m")
        start = max(shift_month(this, -(n - 1)), first.strftime("%Y-%m"))
        out, month = [], start
        while month <= this:
            day = today if month == this else month_end(month)
            w = self.worth_at(day)
            out.append({"month": month, "day": day.isoformat(), "total": w.total, "partial": month == this})
            month = shift_month(month, 1)
        return out

    def change(self, today: date | None = None) -> dict:
        """This month so far against last month's end: {"now", "before", "change", "pct"} (pct None if before is 0)."""
        today = today or datetime.now(timezone.utc).date()
        now = self.worth_at(today)
        before = self.worth_at(today.replace(day=1) - timedelta(days=1))
        diff = now.total - before.total
        pct = (diff / abs(before.total) * 100).quantize(Decimal("0.1")) if before.total else None
        return {"now": now, "before": before, "change": diff, "pct": pct, "had_before": before.counted}

    # --- brokerage holdings -------------------------------------------------------------------------

    def set_holdings(self, account_id: int, holdings: list[dict], as_of: date) -> None:
        """Replace an account's positions with what the brokerage says it holds now."""
        self.db.conn.execute("DELETE FROM holdings WHERE account_id = ?", (account_id,))
        for h in holdings:
            self.db.conn.execute(
                """INSERT INTO holdings (account_id, symbol, name, quantity, price, market_value, cost, currency, as_of)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, h["symbol"], h.get("name"), f"{Decimal(h['quantity']).normalize():f}",
                 str(h["price"]) if h.get("price") is not None else None, str(cents(Decimal(h["market_value"]))),
                 str(cents(Decimal(h["cost"]))) if h.get("cost") is not None else None, h["currency"], as_of.isoformat()))
        self.db.conn.commit()

    def holdings(self, account_id: int | None = None):
        if account_id is None:
            return self.db.conn.execute("SELECT * FROM holdings ORDER BY CAST(market_value AS REAL) DESC").fetchall()
        return self.db.conn.execute("SELECT * FROM holdings WHERE account_id = ? ORDER BY CAST(market_value AS REAL) DESC",
                                    (account_id,)).fetchall()
