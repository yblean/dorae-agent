"""Brokerage accounts that update themselves in your net worth: their value and what they hold.

A broker only has to say which accounts you have and what each is worth now (with its positions).
`sync` turns that into an "investment" account per brokerage account, with today's balance and
holdings. Only reading calls are ever made: nothing here can place or change an order.

Connected so far: Webull SG (doraemon.brokers.webull). IBKR (Flex Web Service) can plug in the same way.
"""
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Protocol

from doraemon.networth import NetWorth

log = logging.getLogger(__name__)


class BrokerError(Exception):
    """Something to tell you, worded without keys or secrets."""


class Broker(Protocol):
    source: str  # stored with the account, e.g. "webull"
    label: str   # shown to you, e.g. "Webull"

    def accounts(self) -> list[dict]:
        """[{"id", "name", "currency"}]"""

    def snapshot(self, account_id: str) -> dict:
        """{"value": Decimal (cash + positions), "currency", "holdings": [{"symbol", "name", "quantity",
        "price", "market_value", "cost", "currency"}]}"""


def sync(nw: NetWorth, broker: Broker, today: date | None = None, fx=None) -> list[str]:
    """Bring every account at `broker` into your net worth. Returns one line per account for you."""
    today = today or datetime.now(timezone.utc).date()
    lines = []
    for acc in broker.accounts():
        snap = broker.snapshot(acc["id"])
        row = nw.find(broker.source, str(acc["id"]))
        if row is None:
            name = f"{broker.label} {acc.get('name') or ''}".strip()
            account_id = nw.add_account(name, "investment", snap["currency"], broker.source, str(acc["id"]))
        else:
            account_id = row["id"]
        nw.set_balance(account_id, Decimal(snap["value"]), today, broker.source)
        nw.set_holdings(account_id, snap["holdings"], today)
        if fx is not None and snap["currency"] != nw.home:
            fx.rate(snap["currency"], nw.home, today)  # online: caches today's rate for the pages
        lines.append(f"{broker.label}: {snap['currency']} {Decimal(snap['value']):,.2f} "
                     f"({len(snap['holdings'])} position{'s' if len(snap['holdings']) != 1 else ''})")
    return lines


def connected(settings) -> list[Broker]:
    """The brokerages you've set up in .env. Problems setting one up are logged, not raised."""
    found = []
    if settings.webull_app_key and settings.webull_app_secret:
        from doraemon.brokers.webull import WebullSG
        try:
            found.append(WebullSG(settings.webull_app_key, settings.webull_app_secret, settings.webull_endpoint))
        except BrokerError as e:
            log.warning("Webull: %s", e)
    return found
