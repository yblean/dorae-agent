"""Foreign-currency spending converted to your home currency (DORAEMON_HOME_CURRENCY).

Rates are European Central Bank reference rates from Frankfurter (no API key),
taken on each purchase's date. Only currency codes and dates are sent, never
amounts or merchants. Rates are fetched as a date range per currency and cached
in the local database, so the web page reads them without going online.
Your bank's rate and fees differ slightly, so converted amounts are approximate.
"""
import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx

from doraemon.schema import Transaction

API = "https://api.frankfurter.dev/v1/{start}..{end}"
RETRY_AFTER_S = 600   # a failed lookup (e.g. offline) is tried again after 10 minutes
WINDOW_DAYS = 14      # fetch this many days around a purchase in one request
MAX_GAP_DAYS = 7      # ECB publishes on business days; weekends use the last earlier rate
Fetcher = Callable[[str, str, date, date], dict[date, Decimal]]


def frankfurter(base: str, quote: str, start: date, end: date) -> dict[date, Decimal]:
    """Daily rates base->quote between start and end (business days only)."""
    for _ in range(2):  # one retry: the service is sometimes slow to answer
        try:
            resp = httpx.get(API.format(start=start.isoformat(), end=end.isoformat()),
                             params={"base": base, "symbols": quote},
                             timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
            if resp.status_code == 404:
                return {}  # currency not covered by the ECB
            resp.raise_for_status()
            return {date.fromisoformat(day): Decimal(str(r[quote]))
                    for day, r in resp.json().get("rates", {}).items() if quote in r}
        except httpx.TransportError:
            continue
        except (httpx.HTTPError, ValueError, KeyError):
            return {}
    return {}


class FxRates:
    def __init__(self, db_path: str | Path = ":memory:", fetch: Fetcher | None = frankfurter) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.db.execute("""CREATE TABLE IF NOT EXISTS fx_rates (
            day TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL, rate TEXT NOT NULL,
            PRIMARY KEY (day, base, quote))""")
        self.db.commit()
        self.fetch = fetch
        self._failed_at: dict[tuple[str, str, str], float] = {}

    def _cached(self, base: str, quote: str, day: date) -> Decimal | None:
        """The rate on `day`, or the last one before it (weekends, holidays)."""
        row = self.db.execute(
            """SELECT rate FROM fx_rates WHERE base = ? AND quote = ? AND day <= ? AND day >= ?
               ORDER BY day DESC LIMIT 1""",
            (base, quote, day.isoformat(), (day - timedelta(days=MAX_GAP_DAYS)).isoformat()),
        ).fetchone()
        return Decimal(row[0]) if row else None

    def rate(self, base: str, quote: str, day: date, online: bool = True) -> Decimal | None:
        if base == quote:
            return Decimal(1)
        today = datetime.now(timezone.utc).date()
        day = min(day, today)  # pay-later charges can be in the future
        if (cached := self._cached(base, quote, day)) is not None:
            return cached
        key = (day.isoformat(), base, quote)
        if not online or self.fetch is None or time.monotonic() - self._failed_at.get(key, -RETRY_AFTER_S) < RETRY_AFTER_S:
            return None
        rates = self.fetch(base, quote, day - timedelta(days=WINDOW_DAYS), min(day + timedelta(days=WINDOW_DAYS), today))
        if rates:
            self.db.executemany("INSERT OR REPLACE INTO fx_rates VALUES (?, ?, ?, ?)",
                                [(d.isoformat(), base, quote, str(r)) for d, r in rates.items()])
            self.db.commit()
        if (cached := self._cached(base, quote, day)) is not None:
            return cached
        self._failed_at[key] = time.monotonic()
        return None

    def to_home(self, txn: Transaction, home: str, online: bool = True) -> Decimal | None:
        """Amount in the home currency, or None if no rate is available."""
        rate = self.rate(txn.currency, home, txn.purchased_at.date(), online)
        if rate is None:
            return None
        return (txn.amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
