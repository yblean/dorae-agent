"""Checking Gmail for new mail: only since the last check, and automatically while the web app is open."""
import dataclasses
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import doraemon.ingest as ingest
from doraemon.db import Database
from doraemon.email_parse import parse_eml
from doraemon.google_auth import NotConnected
from doraemon.gmail import GmailMessage
from doraemon.ingest import CHECKED_AT, run_ingest, search_window
from doraemon.schema import ActionItem, Extraction
from doraemon.web import CheckJob, create_app

FIXTURE = (Path(__file__).parent / "fixtures" / "bill.eml").read_bytes()


def test_first_check_looks_back_30_days_then_only_since_last_check(tmp_path):
    db = Database(tmp_path / "d.db")
    assert search_window(db, None, False) == ("newer_than:30d -in:chats", "in the last 30 days")
    last = datetime.now(timezone.utc) - timedelta(minutes=15)
    db.set_setting(CHECKED_AT, last.isoformat())
    query, _ = search_window(db, None, False)
    since = int(query.split()[0].removeprefix("after:"))
    assert since == int((last - timedelta(hours=1)).timestamp())  # an hour's overlap catches late-arriving mail
    assert search_window(db, 7, False)[0] == "newer_than:7d -in:chats"  # --days still means exactly that


def test_after_a_long_break_looks_back_at_most_30_days(tmp_path):
    db = Database(tmp_path / "d.db")
    db.set_setting(CHECKED_AT, "2020-01-01T00:00:00+00:00")
    since = int(search_window(db, None, False)[0].split()[0].removeprefix("after:"))
    assert since >= int((datetime.now(timezone.utc) - timedelta(days=30, minutes=1)).timestamp())


# --- run_ingest with Gmail and the model faked ------------------------------------

class FakeService:
    def users(self):
        return self

    def getProfile(self, userId):
        return self

    def execute(self):
        return {"emailAddress": "me@example.com"}


@pytest.fixture
def gmail(monkeypatch, settings, tmp_path):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), convert_currencies=False)
    inbox = {"ids": ["g2", "g1"], "queries": [], "fail": None}
    monkeypatch.setattr(ingest, "connect", lambda *a, **k: FakeService())
    monkeypatch.setattr(ingest, "get_backend", lambda *a: type("B", (), {"name": "fake"})())
    monkeypatch.setattr(ingest, "list_ids", lambda service, q, limit: inbox["queries"].append(q) or list(inbox["ids"]))
    monkeypatch.setattr(ingest, "fetch", lambda service, i: GmailMessage(i, "t" + i, ["INBOX"], FIXTURE))

    def process(msg, *a):
        if inbox["fail"]:
            raise inbox["fail"]
        return parse_eml(msg.raw), Extraction(message_id=msg.id)
    monkeypatch.setattr(ingest, "ingest_message", process)
    return s, inbox


def test_reads_only_emails_it_hasnt_seen(gmail):
    s, inbox = gmail
    assert run_ingest(s, say=lambda _: None)["processed"] == 2
    assert Database(s.db_path).get_setting(CHECKED_AT)
    inbox["ids"] = ["g3", "g2", "g1"]  # one new email arrived
    assert run_ingest(s, say=lambda _: None)["processed"] == 1
    assert inbox["queries"][0].startswith("newer_than:30d") and inbox["queries"][1].startswith("after:")


def test_model_not_running_stops_and_keeps_the_window_open(gmail):
    s, inbox = gmail
    inbox["fail"] = httpx.ConnectError("refused")
    summary = run_ingest(s, say=lambda _: None)
    assert summary["processed"] == 0 and summary["counts"]["failed"] == 2
    assert "couldn't reach the model" in summary["stopped"]
    assert Database(s.db_path).get_setting(CHECKED_AT) is None  # so the next check retries them


# --- the web app's automatic check ---------------------------------------------------

def summary(processed=0, items=0, txns=0, **extra):
    return {"processed": processed, "items": items, "transactions": [object()] * txns,
            "counts": {}, "stopped": "", **extra}


def calendar_texts(db):
    return [r["text"] for r in db.messages("calendar")]


def test_automatic_check_speaks_only_when_theres_news(tmp_path, settings):
    db = Database(tmp_path / "d.db")
    result = {"value": summary(processed=3)}
    job = CheckJob(db, settings, ingest=lambda *a, **k: result["value"])
    job.run(auto=True)
    assert calendar_texts(db) == []  # nothing new: no message every 15 minutes
    job.run(auto=False)
    assert calendar_texts(db) == ["Checked 3 new email(s): nothing that needs you."]  # Run now always answers
    result["value"] = summary(processed=2, items=1, txns=1)
    job.run(auto=True)
    assert "1 new thing(s) for you" in calendar_texts(db)[-1]
    assert "1 new payment(s)" in db.messages("money")[-1]["text"]


def test_automatic_check_says_each_problem_once(tmp_path, settings):
    db = Database(tmp_path / "d.db")

    def expired(*a, interactive, **k):
        assert interactive is False  # never opens a browser from the background
        raise NotConnected("expired")
    job = CheckJob(db, settings, ingest=expired)
    job.run(auto=True)
    job.run(auto=True)
    assert len(calendar_texts(db)) == 1 and "calendar_sync connect" in calendar_texts(db)[0]
    job.ingest = lambda *a, **k: summary(processed=1, stopped="couldn't reach the model (ollama). Is it running?")
    job.run(auto=True)
    job.run(auto=True)
    assert len(calendar_texts(db)) == 2 and "Is it running?" in calendar_texts(db)[-1]


def test_new_messages_show_up_without_a_reload(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), google_token=str(tmp_path / "t.json"))
    client = TestClient(create_app(s, ingest=lambda *a, **k: summary(processed=1, items=1)))
    client.get("/chat/calendar")
    last = client.app.state.db.last_message("calendar")["id"]
    assert client.get(f"/chat/calendar/new?after={last}").json() == {"html": "", "last": last}
    client.app.state.job.run(auto=True)
    res = client.get(f"/chat/calendar/new?after={last}").json()
    assert "1 new thing(s) for you" in res["html"] and res["last"] > last
    assert "Every 15 min" in client.get("/chat/calendar").text


def test_web_checks_never_print_to_the_console(gmail, monkeypatch):
    # a Windows console can't encode "✉": printing from the background check crashed it
    s, inbox = gmail
    bill = parse_eml(FIXTURE)
    found = Extraction(message_id="g1", items=[ActionItem(
        message_id="g1", type="bill", title="Electricity", start_at=None, end_at=None, all_day=True,
        timezone="UTC", evidence_snippet="", confidence=0.9)])
    monkeypatch.setattr(ingest, "ingest_message", lambda msg, *a: (bill, found))

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))  # like the console
    assert run_ingest(s, say=lambda _: None)["items"] == 2
