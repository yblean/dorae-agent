from datetime import datetime, timezone

import pytest

from doraemon.email_parse import ParsedEmail, gmail_categories, parse_eml
from doraemon.triage import skip_reason


def email(subject, categories=()):
    return ParsedEmail(message_id="m", subject=subject, sender="x@y.com",
                       sent_at=datetime(2026, 10, 1, tzinfo=timezone.utc), body="",
                       gmail_categories=frozenset(categories))


def test_categories_from_api_and_takeout():
    assert gmail_categories(["INBOX", "CATEGORY_PROMOTIONS", "UNREAD"]) == {"promotions"}
    assert gmail_categories(["Archived", "Category promotions", "Opened"]) == {"promotions"}


def test_takeout_header_is_parsed():
    raw = b"Subject: Sale\nX-Gmail-Labels: Archived,Category promotions,Opened\n\nhi"
    assert parse_eml(raw).gmail_categories == {"promotions"}


@pytest.mark.parametrize("subject, categories", [
    ("FLASH DEAL: $2 off PlayMade", ["promotions"]),
    ("<ADV> Unlock up to three NVIDIA shares", ["updates"]),
    ("You've been invited", ["social"]),
    ("[EDM] Tech Unlocked - Big Discovery at NCS Hub", ["updates"]),
])
def test_marketing_is_skipped(subject, categories):
    assert skip_reason(email(subject, categories))


@pytest.mark.parametrize("subject, categories", [
    ("Your order #2610 has been shipped", ["promotions"]),  # Gmail misfiled it
    ("Booking confirmation: Hotel S-Plus", ["promotions"]),
    ("MUST READ - Fee Statement and Due Date", ["updates"]),
    ("Transaction Alerts", []),
])
def test_real_mail_goes_through(subject, categories):
    assert skip_reason(email(subject, categories)) is None
