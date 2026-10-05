"""Data shapes for the Inbox to Action pipeline.

Raw* models are what the LLM returns. They keep dates as the text written in
the email; doraemon.dates turns that text into datetimes, so the model never
does date math.
"""
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class ItemType(StrEnum):
    BILL = "bill"
    APPOINTMENT = "appointment"
    DEADLINE = "deadline"
    DELIVERY = "delivery"
    RSVP = "rsvp"
    FLIGHT = "flight"
    HOTEL = "hotel"
    OTHER_TRAVEL = "other_travel"


class Category(StrEnum):
    GROCERIES = "groceries"
    DINING = "dining"
    SHOPPING = "shopping"
    TRANSPORT = "transport"
    TRAVEL = "travel"
    UTILITIES = "utilities"
    SUBSCRIPTIONS = "subscriptions"
    MEMBERSHIPS = "memberships"
    HEALTH = "health"
    ENTERTAINMENT = "entertainment"
    OTHER = "other"


class TxnSource(StrEnum):
    BANK_ALERT = "bank_alert"              # bank, card or payment-app notification
    MERCHANT_RECEIPT = "merchant_receipt"  # receipt from the shop or service itself


class Status(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    UNDONE = "undone"


# --- What the model returns -------------------------------------------------

class RawItem(BaseModel):
    type: ItemType
    title: str = Field(description="Short title, e.g. 'Pay electricity bill'")
    departs_from: str | None = Field(
        description="Flights and other_travel only: the place printed directly next to the departure "
        "time, copied exactly (e.g. 'Changi Airport'). Otherwise null."
    )
    when: str | None = Field(description="Start or due date/time copied exactly as written in the email, or null")
    end: str | None = Field(description="End date/time as written (hotel checkout, flight arrival), or null")
    timezone: str | None = Field(
        description="Flights and other_travel only: IANA timezone of `departs_from`, "
        "e.g. 'Asia/Tokyo' for Narita, 'Asia/Singapore' for Changi. Otherwise null."
    )
    location: str | None
    booking_ref: str | None
    amount: float | None
    currency: str | None = Field(description="ISO 4217 code, e.g. USD")
    evidence: str = Field(description="Short exact quote from the email that supports this item")
    confidence: float = Field(ge=0, le=1)


class RawTransaction(BaseModel):
    source: TxnSource
    merchant: str = Field(description="Who was paid, as named in the email")
    order_ref: str | None
    purchased: str | None = Field(description="Purchase date as written in the email, or null")
    amount: float = Field(description="Total charged (or refunded), positive number")
    currency: str | None = Field(description="ISO 4217 code, e.g. USD")
    category: Category
    is_refund: bool


class RawExtraction(BaseModel):
    items: list[RawItem]
    transactions: list[RawTransaction]


# --- What the pipeline produces ---------------------------------------------

class ActionItem(BaseModel):
    message_id: str
    type: ItemType
    title: str
    start_at: datetime | None
    end_at: datetime | None
    all_day: bool
    timezone: str  # the event's own timezone; for flights the model's guess, shown for review
    departs_from: str | None = None
    location: str | None = None
    booking_ref: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    date_text: str | None = None  # original wording, shown on the review card
    evidence_snippet: str = Field(max_length=200)
    confidence: float
    status: Status = Status.PROPOSED


class Transaction(BaseModel):
    message_id: str
    source: TxnSource
    merchant: str
    order_ref: str | None
    purchased_at: datetime
    amount: Decimal
    currency: str
    amount_home: Decimal | None  # None until FX conversion exists
    category: Category
    is_refund: bool


class Extraction(BaseModel):
    message_id: str
    items: list[ActionItem] = []
    transactions: list[Transaction] = []
    problems: list[str] = []
    applied_rules: list[str] = []  # what the rules store changed, for the review card and the eval
    skipped: str | None = None  # why the model wasn't run (triage or an ignore rule)
    latency_s: float = 0.0

    @property
    def actionable(self) -> bool:
        return bool(self.items)
