"""Running Doraemon's jobs in one program at a time, Telegram buttons and the briefing on Telegram."""
import dataclasses
import time
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon import jobs, service
from doraemon.email_parse import ParsedEmail
from doraemon.jobs import FileLock, Runtime
from doraemon.schema import ActionItem, Extraction
from doraemon.web import create_app

TZ = "Asia/Singapore"


class FakeBot:
    chat_id = "42"

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send(self, text, buttons=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append(text)


@pytest.fixture
def settings(tmp_path, settings):
    return dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone=TZ, home_currency="SGD",
                               poll_minutes=0, google_token=str(tmp_path / "t.json"),
                               google_credentials=str(tmp_path / "c.json"))


def add_bill(rt, title="SP electricity", days=2, status="confirmed"):
    now = datetime.now(ZoneInfo(TZ))
    item = ActionItem(message_id="m", type="bill", title=title, start_at=now + timedelta(days=days), end_at=None,
                      all_day=True, timezone=TZ, amount=Decimal("80.50"), currency="SGD", evidence_snippet="",
                      confidence=0.9)
    email = ParsedEmail(message_id="m", subject="s", sender="SP <a@sp.sg>", sent_at=now, body="")
    rt.db.save_result(f"g-{title}", "t", email, Extraction(message_id="m", items=[item]), "extracted")
    item_id = rt.db.conn.execute("SELECT MAX(id) FROM action_items").fetchone()[0]
    if status == "confirmed":
        rt.db.update("item", item_id, "confirm", status="confirmed")
    return item_id


def wait_for(check, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


# --- one program at a time ---------------------------------------------------------------

def test_only_one_holder_of_the_lock(tmp_path):
    a, b = FileLock(tmp_path / "jobs.lock"), FileLock(tmp_path / "jobs.lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_the_service_gets_the_jobs_and_the_web_app_takes_them_back(settings, monkeypatch):
    monkeypatch.setattr(jobs, "HEARTBEAT", 0.05)
    web, svc = Runtime(settings), Runtime(settings)
    stop_web = web.supervise("web app")  # the web app was open first...
    assert wait_for(lambda: (web.owner() or {}).get("by") == "web app")
    stop_svc = svc.supervise("background service")
    assert wait_for(lambda: (svc.owner() or {}).get("by") == "background service")  # ...and hands over
    assert web.stops == [] and svc.stops
    time.sleep(0.2)
    assert svc.owner()["by"] == "background service"  # and stays out of the way
    stop_svc.set()
    assert wait_for(lambda: (web.owner() or {}).get("by") == "web app", seconds=5)  # back when the service stops
    stop_web.set()
    assert wait_for(lambda: web.owner() is None)


def test_test_button_says_who_answered(settings):
    rt = Runtime(settings)
    rt.name = "background service"
    assert rt.button("test:1") == "👍 Buttons work. The background service answered."


def test_a_check_already_running_elsewhere_is_not_started_twice(settings):
    ran = []
    first, second = Runtime(settings, ingest=lambda *a, **k: ran.append(1)), Runtime(settings)
    assert first.check.across.acquire()  # as if the other program were mid-check
    assert not second.check.start()
    first.check.across.release()


# --- buttons under Telegram reminders ------------------------------------------------------

def test_done_button_stops_reminders_and_tells_dorae2(settings):
    rt = Runtime(settings)
    item_id = add_bill(rt)
    assert rt.button(f"done:{item_id}") == "✅ Done: SP electricity"
    assert rt.db.get("item", item_id)["done_at"]
    assert rt.db.last_message("calendar")["text"] == "✅ Done: SP electricity. (You pressed Done on Telegram.)"
    assert rt.brain.agenda(7, ("bill",)) == []  # done bills leave the briefing
    assert rt.button("snooze:abc") == "I don't know that button."


def test_snooze_button(settings):
    rt = Runtime(settings)
    item_id = add_bill(rt)
    assert rt.button(f"snooze:{item_id}").startswith("⏰ Snoozed: I'll remind you again ")  # at 5pm, or Sun 18 Oct at 12:30am
    assert [r["label"] for r in rt.db.reminders(item_id)] == ["snoozed"]


# --- the briefing on Telegram --------------------------------------------------------------

def test_briefing_goes_to_telegram_too(settings):
    bot = FakeBot()
    rt = Runtime(dataclasses.replace(settings, briefing_time="00:00"), messenger=bot)
    add_bill(rt)
    assert rt.briefing_tick()
    text = bot.sent[0]
    assert text.startswith("☀️ Good ") and "\nToday\n• Nothing on" in text
    assert "Bills due soon\n• SP electricity · " in text and "SGD 80.50" in text
    assert not rt.briefing_tick() and len(bot.sent) == 1  # once a day


def test_briefing_telegram_can_be_off_and_failures_are_said(settings):
    bot = FakeBot()
    off = Runtime(dataclasses.replace(settings, briefing_time="00:00", briefing_telegram=False), messenger=bot)
    assert off.briefing_tick() and bot.sent == []
    off.db.set_setting("overview:calendar", None)
    broken = Runtime(dataclasses.replace(settings, briefing_time="00:00"), messenger=FakeBot(fail="network down"))
    assert broken.briefing_tick()
    assert "couldn't send this morning's briefing on Telegram (network down)" in broken.db.last_message("calendar")["text"]


# --- the web app ---------------------------------------------------------------------------

def test_mark_done_on_upcoming_and_who_runs_the_jobs(settings):
    client = TestClient(create_app(settings), follow_redirects=True)
    rt = client.app.state.runtime
    item_id = add_bill(rt)
    assert "Mark done" in client.get("/upcoming").text
    page = client.post(f"/items/{item_id}/done", data={"back": "/upcoming"}).text
    assert "✅ Done: SP electricity" in page and "✓ Done" in page
    assert "Not running: no reminders or briefing will go out" in client.get("/chat/calendar").text
    rt.name = "background service"
    rt.heartbeat()
    assert "Running in the background, even with this page closed" in client.get("/chat/calendar").text


# --- starting at login -----------------------------------------------------------------------

def test_launcher_starts_the_service_hidden_from_the_project_folder():
    script = service.launcher_script()
    assert f'shell.CurrentDirectory = "{service.PROJECT}"' in script
    assert '-m doraemon.service", 0, False' in script  # 0: no window


def test_status_says_when_nothing_runs_the_jobs(settings):
    assert "Nothing is running Doraemon's jobs" in service.status(settings)


def test_the_service_log_never_contains_the_bot_token(settings, tmp_path):
    import logging
    s = dataclasses.replace(settings, telegram_token="123:SECRET")
    path = service.log_to_file(s)
    root = logging.getLogger()
    try:
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot123:SECRET/getUpdates")
        logging.getLogger("doraemon").warning("failed calling %s", "https://api.telegram.org/bot123:SECRET/x")
    finally:
        for h in root.handlers[-2:]:
            if getattr(h, "baseFilename", None) == str(path) or isinstance(h, logging.StreamHandler):
                h.close()
                root.removeHandler(h)
    text = path.read_text(encoding="utf-8")
    assert "SECRET" not in text and "bot<token>/x" in text and "getUpdates" not in text


def test_data_paths_dont_depend_on_the_folder_doraemon_starts_in(tmp_path, monkeypatch):
    from doraemon.config import PROJECT, in_project
    monkeypatch.chdir(tmp_path)  # e.g. started from .venv/Scripts
    assert in_project("data/doraemon.db") == str(PROJECT / "data/doraemon.db")
    assert in_project(str(tmp_path / "x.db")) == str(tmp_path / "x.db")  # absolute paths stay as they are
