import json
from pathlib import Path

from conftest import FakeBackend
from doraemon.db import Database
from doraemon.gmail import GmailMessage
from doraemon.ingest import ingest_message, triage_label
from doraemon.rules import RuleStore

FIXTURE = (Path(__file__).parent / "fixtures" / "bill.eml").read_bytes()
BILL = {"items": [{"type": "bill", "title": "Pay electricity bill", "departs_from": None,
                   "when": "Friday, October 16, 2026", "end": None, "timezone": None, "location": None,
                   "booking_ref": None, "amount": 84.2, "currency": "SGD", "evidence": "due", "confidence": 0.9}],
        "transactions": []}


def test_gmail_labels_drive_triage(settings):
    backend = FakeBackend(json.dumps(BILL))
    promo = GmailMessage("g1", "t1", ["INBOX", "CATEGORY_PROMOTIONS"], FIXTURE.replace(
        b"Your October electricity bill is ready", b"Big sale this weekend"))
    _, result = ingest_message(promo, backend, settings, RuleStore(use_defaults=False))
    assert result.skipped == "Gmail promotions tab"
    assert backend.calls == []  # the model never saw it
    assert triage_label(result) == "skipped: Gmail promotions tab"


def test_real_email_is_extracted(settings):
    msg = GmailMessage("g2", "t2", ["INBOX", "CATEGORY_UPDATES"], FIXTURE)
    email, result = ingest_message(msg, FakeBackend(json.dumps(BILL)), settings, RuleStore(use_defaults=False))
    assert email.subject == "Your October electricity bill is ready"
    assert triage_label(result) == "extracted"
    assert result.items[0].start_at.date().isoformat() == "2026-10-16"


FORWARD = (b"Subject: Fwd: Ticket issuance confirmed\nFrom: Matthew <m@x.com>\nTo: yibin@x.com\n"
           b"Date: Fri, 18 Sep 2026 10:00:00 +0800\n\n---------- Forwarded message ---------\n"
           b"From: airline <a@ceair.com>\nTo: <m@x.com>\n\nTotal SGD 1579.50")
TICKET = {"items": [], "transactions": [{"source": "merchant_receipt", "merchant": "China Eastern", "order_ref": None,
                                          "purchased": None, "amount": 1579.5, "currency": "SGD",
                                          "category": "travel", "is_refund": False}]}


def test_friends_forwarded_booking_is_not_your_spending(settings):
    import dataclasses
    mine = dataclasses.replace(settings, user_emails=("yibin@x.com",))
    msg = GmailMessage("g3", "t3", ["INBOX"], FORWARD)
    _, result = ingest_message(msg, FakeBackend(json.dumps(TICKET)), mine, RuleStore(use_defaults=False))
    assert result.transactions == []
    assert "forwarded booking was for m@x.com" in result.applied_rules[0]
    # Forwarded by you, originally sent to you: still counts
    own = FORWARD.replace(b"To: <m@x.com>", b"To: <yibin@x.com>")
    _, result = ingest_message(GmailMessage("g4", "t4", [], own), FakeBackend(json.dumps(TICKET)), mine,
                               RuleStore(use_defaults=False))
    assert len(result.transactions) == 1


def test_processed_emails_are_remembered(tmp_path):
    db = Database(tmp_path / "d.db")
    assert not db.is_processed("g1")
    db.mark_processed("g1", "t1", "extracted")
    assert Database(tmp_path / "d.db").is_processed("g1")
