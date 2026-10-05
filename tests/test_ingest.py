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
    subject, result = ingest_message(msg, FakeBackend(json.dumps(BILL)), settings, RuleStore(use_defaults=False))
    assert subject == "Your October electricity bill is ready"
    assert triage_label(result) == "extracted"
    assert result.items[0].start_at.date().isoformat() == "2026-10-16"


def test_processed_emails_are_remembered(tmp_path):
    db = Database(tmp_path / "d.db")
    assert not db.is_processed("g1")
    db.mark_processed("g1", "t1", "extracted")
    assert Database(tmp_path / "d.db").is_processed("g1")
