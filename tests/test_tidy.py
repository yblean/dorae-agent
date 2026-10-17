"""Keeping "needs your OK" short (doraemon.tidy), and noticing when something stops working (doraemon.jobs)."""
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from doraemon import jobs, llm
from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.ingest import without_twins
from doraemon.jobs import CheckJob, Runtime
from doraemon.schema import ActionItem, Extraction
from doraemon.tidy import dismiss_twins, expire_passed, same_item, tidy

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=SG)


def item(title="MU2048 Singapore to Kunming", type="flight", start=datetime(2026, 11, 5, 6, 30, tzinfo=SG),
         all_day=False, **extra):
    return ActionItem(message_id="m", type=type, title=title, start_at=start, end_at=None, all_day=all_day,
                      timezone="Asia/Singapore", evidence_snippet="", confidence=0.9, **extra)


def save(db, gmail_id, *items, status="proposed"):
    email = ParsedEmail(message_id="m", subject="s", sender="a@b.c", sent_at=NOW, body="")
    db.save_result(gmail_id, "t", email, Extraction(message_id="m", items=list(items)), "extracted")
    ids = [r[0] for r in db.conn.execute("SELECT id FROM action_items WHERE gmail_id = ?", (gmail_id,))]
    if status != "proposed":
        for i in ids:
            db.update("item", i, "confirm", status=status)
    return ids


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "d.db")


# --- the same thing twice ------------------------------------------------------------------

@pytest.mark.parametrize("a, b, same", [
    (item(), item("MU2048 SIN -> KMG"), True),  # a forward of the e-ticket
    (item(), item("MU2048 SIN -> KMG", start=datetime(2026, 11, 5, 7, 30, tzinfo=SG)), False),  # another time
    (item(), item("MU2047 KMG -> SIN"), False),
    (item("Tiger Leaping Gorge Manor", "hotel", all_day=True), item("Tiger Leaping Gorge Manor", "hotel", all_day=True), True),
    (item("Kiro Workshop", "rsvp"), item("AWS Workshop", "rsvp"), False),  # one shared word isn't enough
    (item("Hotel", "hotel", booking_ref="A1"), item("Hotel", "hotel", booking_ref="B2"), False),
    (item(), item(type="appointment"), False),
])
def test_same_item(a, b, same):
    assert same_item(a, b) is same


def test_a_new_email_leaves_out_what_another_email_gave(db):
    save(db, "g1", item())
    result = without_twins(db, Extraction(message_id="m", items=[item("MU2048 SIN -> KMG"), item("MU2047 KMG -> SIN")]),
                           "g2")
    assert [i.title for i in result.items] == ["MU2047 KMG -> SIN"]
    assert result.problems == ["left out 1 item(s) already found in another email"]


def test_rereading_the_same_email_isnt_a_twin_of_itself(db):
    save(db, "g1", item())
    assert len(without_twins(db, Extraction(message_id="m", items=[item()]), "g1").items) == 1


def test_waiting_twins_are_dismissed_and_the_confirmed_one_kept(db):
    (later,) = save(db, "g2", item("MU2048 SIN -> KMG"))
    (first,) = save(db, "g1", item(), status="confirmed")
    assert dismiss_twins(db) == ["MU2048 SIN -> KMG"]
    assert db.get("item", first)["status"] == "confirmed" and db.get("item", later)["status"] == "dismissed"
    db.undo(db.recent_actions(1)[0]["id"])  # it's in Activity
    assert db.get("item", later)["status"] == "proposed"


# --- suggestions that passed -----------------------------------------------------------------

def test_suggestions_a_day_past_their_date_are_dismissed(db):
    old, yesterday, soon = (save(db, f"g{n}", item(t, "deadline", start=d, all_day=True))[0] for n, (t, d) in enumerate([
        ("FPL Gameweek 4 deadline", datetime(2026, 9, 12, tzinfo=SG)),
        ("Submit log report", datetime(2026, 10, 16, tzinfo=SG)),
        ("Register for mentorship", datetime(2026, 10, 25, tzinfo=SG))]))
    (confirmed,) = save(db, "g9", item("Old confirmed", "deadline", start=datetime(2026, 9, 1, tzinfo=SG), all_day=True),
                        status="confirmed")
    assert expire_passed(db, "Asia/Singapore", NOW) == ["FPL Gameweek 4 deadline"]
    assert [db.get("item", i)["status"] for i in (old, yesterday, soon, confirmed)] == \
        ["dismissed", "proposed", "proposed", "confirmed"]


def test_tidy_says_what_it_cleared(db):
    save(db, "g1", item(), item("Survey", "deadline", start=datetime(2026, 9, 9, tzinfo=SG), all_day=True))
    save(db, "g2", item("MU2048 SIN -> KMG"))
    assert tidy(db, "Asia/Singapore", NOW) == ("🧹 I cleared 1 that repeated another item (“MU2048 SIN -> KMG”) and 1 "
                                               "that passed without your OK (“Survey”). Undo any of them under Activity.")
    assert tidy(db, "Asia/Singapore", NOW) == ""
    save(db, "g3", item("Quiz", "deadline", start=datetime(2026, 9, 9, tzinfo=SG), all_day=True))
    assert tidy(db, "Asia/Singapore", NOW, expire=False) == ""  # DORAEMON_EXPIRE_PASSED=off


# --- the model, sign-ins and /status ----------------------------------------------------------

class FakeBot:
    chat_id = "42"

    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)


@pytest.fixture
def settings(tmp_path, settings):
    return dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone="Asia/Singapore",
                               home_currency="SGD", poll_minutes=0, google_token=str(tmp_path / "t.json"),
                               google_credentials=str(tmp_path / "c.json"))


def test_ollama_is_started_when_its_not_running(settings, monkeypatch, tmp_path):
    started, up = [], iter([False, False, True])
    monkeypatch.setattr(llm, "ollama_up", lambda url, wait=2: next(up))
    monkeypatch.setattr(llm, "ollama_exe", lambda: tmp_path / "ollama.exe")
    monkeypatch.setattr(llm.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    s = dataclasses.replace(settings, model="ollama:qwen3.5:4b")
    assert llm.model_ready(s) and started == [[str(tmp_path / "ollama.exe"), "serve"]]
    monkeypatch.setattr(llm, "ollama_up", lambda url, wait=2: False)
    assert not llm.model_ready(dataclasses.replace(s, start_ollama=False), wait=0)
    assert not llm.model_ready(dataclasses.replace(s, ollama_url="http://gpu-box:11434"), wait=0)  # not ours to start
    assert len(started) == 1


def test_a_check_without_the_model_says_so_once_and_on_telegram(settings):
    db = Database(settings.db_path)
    told, ran = [], []
    job = CheckJob(db, settings, ingest=lambda *a, **k: ran.append(1))
    job.ready, job.on_problem = (lambda: False), told.append
    job.run(auto=True)
    job.run(auto=True)
    assert ran == [] and len(told) == 1 and told[0].startswith("I couldn't check your inbox: the model")
    assert db.last_message("calendar")["text"] == told[0]


def test_broker_problems_reach_telegram(settings):
    from tests.test_networth import FakeBroker
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot, brokers=[FakeBroker(fail="Webull said no (HTTP 401)")])
    rt.sync_brokers()
    rt.sync_brokers()
    assert bot.sent == ["⚠️ I couldn't update your Webull account: Webull said no (HTTP 401)."]


def test_webull_sign_in_warning_comes_once_a_few_days_before(settings, monkeypatch):
    bot = FakeBot()
    rt = Runtime(dataclasses.replace(settings, webull_app_key="k", webull_app_secret="s"), messenger=bot)
    ends = datetime(2026, 10, 31, 14, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(rt, "webull_expires", lambda: ends)
    assert not rt.session_tick(ends - timedelta(days=5))
    assert rt.session_tick(ends - timedelta(days=2))
    assert not rt.session_tick(ends - timedelta(days=1))
    assert bot.sent == ["⚠️ Your Webull sign-in runs out on Sat 31 Oct. To keep your investments up to date, run: "
                        "python -m doraemon.brokers.webull, then approve it in the Webull app."]


def test_status_lists_each_part(settings, monkeypatch):
    monkeypatch.setattr(jobs, "ollama_up", lambda url, wait=2: False)
    rt = Runtime(dataclasses.replace(settings, model="ollama:qwen3.5:4b"))
    rt.name = "background service"
    rt.db.set_setting("gmail_checked_at", (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat())
    text = rt.status_text()
    assert text.splitlines()[:5] == ["🩺 Doraemon status", "Running in the background service",
                                     "✅ Gmail: checked 12 min ago", "⚠️ Model (qwen3.5:4b): not running",
                                     "⚠️ Google Calendar: not connected (python -m doraemon.calendar_sync connect)"]
    assert "🔔 Reminders: 0 item(s)" in text and "📬 Waiting for your OK: 0" in text
    assert rt.commands.lookup("/status").answer().startswith("🩺 Doraemon status")


def test_a_misread_end_date_doesnt_make_a_coming_flight_look_over(db):
    flight = item().model_copy(update={"end_at": datetime(2026, 5, 11, 10, 30, tzinfo=SG)})
    save(db, "g1", flight)
    assert expire_passed(db, "Asia/Singapore", NOW) == []
