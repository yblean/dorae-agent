"""Webull Singapore, through Webull's official OpenAPI (read-only use).

Setup: apply for OpenAPI access in Webull (approval takes a few business days; you need an active
account), generate an App Key and App Secret, then put them in .env:

    DORAEMON_WEBULL_APP_KEY=...
    DORAEMON_WEBULL_APP_SECRET=...
    pip install webull-openapi-python-sdk

The keys could place trades, so Doraemon only ever calls the account list, balance and positions,
and never logs them. Webull's SDK signs each request. Its field names vary between versions, so the
reading below looks for the usual names; `python -m doraemon.brokers.webull` prints what your account
returns, to check it before relying on it.
"""
import json
import sys
from decimal import Decimal, InvalidOperation

from doraemon.brokers import BrokerError

ENDPOINT = "api.webull.com.sg"


def _num(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def pick(data, *names):
    """The first of `names` found in `data` or any dict inside it (Webull nests and renames fields)."""
    if isinstance(data, dict):
        for n in names:
            if data.get(n) not in (None, ""):
                return data[n]
        for v in data.values():
            found = pick(v, *names)
            if found is not None:
                return found
    elif isinstance(data, list):
        for v in data:
            found = pick(v, *names)
            if found is not None:
                return found
    return None


def rows(data, *names) -> list[dict]:
    """The list of records under any of `names` (or `data` itself if it's a list)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    found = pick(data, *names)
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def holding(r: dict, currency: str) -> dict:
    qty = _num(pick(r, "quantity", "qty", "position", "holding_qty")) or Decimal(0)
    price = _num(pick(r, "last_price", "market_price", "price", "close_price"))
    value = _num(pick(r, "market_value", "marketValue", "position_market_value"))
    if value is None and price is not None:
        value = qty * price
    cost = _num(pick(r, "cost", "total_cost", "cost_basis"))
    if cost is None and (unit := _num(pick(r, "unit_cost", "cost_price", "avg_price"))) is not None:
        cost = unit * qty
    return {"symbol": str(pick(r, "symbol", "ticker", "instrument_symbol") or "?"),
            "name": pick(r, "name", "instrument_name", "short_name"), "quantity": qty, "price": price,
            "market_value": value or Decimal(0), "cost": cost, "currency": str(pick(r, "currency") or currency)}


class WebullSG:
    source, label = "webull", "Webull"

    def __init__(self, app_key: str, app_secret: str, endpoint: str = ENDPOINT, client=None) -> None:
        if client is None:
            try:
                from webull.core.client import ApiClient
                from webull.trade.trade_client import TradeClient
            except ImportError:
                raise BrokerError("Webull's SDK isn't installed: pip install webull-openapi-python-sdk") from None
            api = ApiClient(app_key, app_secret, "sg")
            api.add_endpoint("sg", endpoint or ENDPOINT)
            client = TradeClient(api)
        self.client = client

    def _call(self, names: tuple[str, ...], **params):
        """Call the first method that exists (the SDK renamed some between versions); return its JSON."""
        for area in ("account_v2", "account"):
            group = getattr(self.client, area, None)
            for name in names:
                method = getattr(group, name, None) if group is not None else None
                if method is None:
                    continue
                try:
                    res = method(**params)
                except Exception as e:  # the SDK's errors can include request details: keep only the type
                    raise BrokerError(f"Webull didn't answer {name} ({type(e).__name__})") from None
                status = getattr(res, "status_code", 200)
                if status != 200:
                    raise BrokerError(f"Webull said no to {name} (HTTP {status}); check your App Key and access")
                return res.json() if hasattr(res, "json") else res
        raise BrokerError(f"This Webull SDK has none of {', '.join(names)}: update webull-openapi-python-sdk")

    def accounts(self) -> list[dict]:
        data = self._call(("get_account_list", "get_app_subscriptions"))
        out = []
        for r in rows(data, "data", "accounts", "account_list"):
            account_id = pick(r, "account_id", "accountId", "id")
            if account_id is not None:
                out.append({"id": str(account_id), "name": str(pick(r, "account_label", "account_type", "account_class") or ""),
                            "currency": str(pick(r, "currency", "base_currency") or "USD")})
        return out

    def snapshot(self, account_id: str) -> dict:
        balance = self._call(("get_account_balance", "get_account_balances"), account_id=account_id)
        currency = str(pick(balance, "total_asset_currency", "currency", "base_currency") or "USD")
        positions = self._call(("get_account_position", "get_account_positions", "get_positions"), account_id=account_id)
        holdings = [holding(r, currency) for r in rows(positions, "holdings", "positions", "data", "items")]
        value = _num(pick(balance, "total_asset", "net_liquidation", "total_net_liquidation", "net_asset", "total_value"))
        if value is None:  # no total given: cash plus what the positions are worth
            cash = _num(pick(balance, "total_cash_balance", "cash_balance", "cash")) or Decimal(0)
            value = cash + sum((h["market_value"] for h in holdings), Decimal(0))
        return {"value": value, "currency": currency, "holdings": holdings}


def main() -> None:
    """Print what Webull returns for your account (to check the fields), without saving anything."""
    from doraemon.config import Settings
    s = Settings()
    if not (s.webull_app_key and s.webull_app_secret):
        sys.exit("Set DORAEMON_WEBULL_APP_KEY and DORAEMON_WEBULL_APP_SECRET in .env first.")
    try:
        broker = WebullSG(s.webull_app_key, s.webull_app_secret, s.webull_endpoint)
        for acc in broker.accounts():
            print(json.dumps({"account": acc, "snapshot": broker.snapshot(acc["id"])}, default=str, indent=2))
    except BrokerError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
