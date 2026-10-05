"""Label file format: data/labels/<name>.json next to data/emails/<name>.eml.

Example:
{
  "reviewed": true,
  "items": [
    {"type": "bill", "title": "Electricity bill", "start_at": "2026-10-16", "amount": "84.20", "currency": "SGD"}
  ],
  "transactions": [
    {"source": "bank_alert", "merchant": "NTUC FP-BT BATOK EAST", "amount": "3.85",
     "category": "groceries", "purchased_at": "2026-08-25"}
  ],
  "notes": ""
}

start_at is "YYYY-MM-DD" for all-day items, or "YYYY-MM-DDTHH:MM" in your
local timezone when the time matters. A non-actionable email has empty lists.

Label what this one email says, including a merchant receipt that a bank
alert also covers. Dropping the duplicate is the ledger's job, and the eval
checks that separately.
"""
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from doraemon.schema import Category, ItemType, Transaction, TxnSource


class LabelItem(BaseModel):
    type: ItemType
    title: str = ""
    start_at: str | None = None
    amount: Decimal | None = None
    currency: str | None = None


class LabelTransaction(BaseModel):
    source: TxnSource
    merchant: str = ""
    amount: Decimal
    currency: str = "SGD"
    category: Category
    is_refund: bool = False
    purchased_at: date

    def to_transaction(self, message_id: str, tz: str) -> Transaction:
        return Transaction(
            message_id=message_id, source=self.source, merchant=self.merchant, order_ref=None,
            purchased_at=datetime.combine(self.purchased_at, time(12), ZoneInfo(tz)),
            amount=self.amount, currency=self.currency, amount_home=None,
            category=self.category, is_refund=self.is_refund,
        )


class Label(BaseModel):
    reviewed: bool = False  # drafts from draft_labels.py start False; eval skips them
    items: list[LabelItem] = []
    transactions: list[LabelTransaction] = []
    notes: str = ""


def load_label(path: Path) -> Label:
    return Label.model_validate_json(path.read_text(encoding="utf-8"))
