import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.reminders import ReminderJob, list_text, message_text, plan, send_due, split_request
from doraemon.schema import ActionItem, Extraction
from doraemon.telegram import Command, CommandListener, Telegram, TelegramError
from doraemon.web import create_app

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=SG)


def item(type="bill", title="Electricity bill", start=datetime(2026, 10, 15, tzinfo=SG), all_day=True, **extra):
    return ActionItem(message_id="m", type=type, title=title, start_at=start, end_at=None, all_day=all_day,
                      timezone="Asia/Singapore", evidence_snippet="Please pay by 15 Oct.", confidence=0.9, **extra)


class FakeBot:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send(self, text):
        if self.fail:
            raise TelegramError(self.fail)
        self.sent.append(text)


def times(planned):
    return [(r["at"].astimezone(SG).strftime("%d %H:%M"), r["label"]) for r in planned]


# --- when reminders go out ---------------------------------------------------------

def test_timed_reminders_step_down_from_the_first():
    exam = item("deadline", "Module registration", datetime(2026, 10, 15, 14, 0, tzinfo=SG), all_day=False)
    planned, passed = plan(exam, "Asia/Singapore", "1d", 3, NOW)
    assert times(planned) == [("14 14:00", "1 day before"), ("15 11:00", "3 hours before"), ("15 13:00", "1 hour before")]
    assert passed == []


def test_all_day_reminders_come_at_9am():
    planned, _ = plan(item(), "Asia/Singapore", "1d", 2, NOW)
    assert times(planned) == [("14 09:00", "the day before"), ("15 09:00", "that morning")]


def test_hours_before_an_all_day_item_means_that_morning():
    planned, _ = plan(item(), "Asia/Singapore", "3h", 2, NOW)
    assert times(planned) == [("15 09:00", "that morning")]  # nothing after it on the ladder


def test_times_that_already_passed_are_dropped():
    planned, passed = plan(item(), "Asia/Singapore", "1w", 3, NOW)  # 1 week before was 8 Oct
    assert passed == ["1 week before"]
    assert [label for _, label in times(planned)] == ["3 days before", "the day before"]


def test_message_says_when_and_links_the_email():
    text = message_text(item(amount=80.2, currency="SGD"), "g9", "Asia/Singapore",
                        datetime(2026, 10, 14, 9, 0, tzinfo=SG))
    assert text.splitlines()[:3] == ["🔔 Electricity bill", "Due tomorrow, Thu 15 Oct", "Amount: SGD 80.2"]
    assert "mail.google.com" in text and "#all/g9" in text


def test_message_counts_hours_for_timed_items():
    exam = item("deadline", "Module registration", datetime(2026, 10, 15, 14, 0, tzinfo=SG), all_day=False)
    text = message_text(exam, "chat:abc", "Asia/Singapore", datetime(2026, 10, 15, 11, 0, tzinfo=SG))
    assert "Due today, Thu 15 Oct at 2pm (in 3 hours)" in text
    assert "mail.google.com" not in text  # added in chat, no email to link


# --- sending -----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / "d.db")
    email = ParsedEmail(message_id="m", subject="Bill", sender="SP <bill@sp.com.sg>", sent_at=NOW, body="")
    db.save_result("g1", "t1", email, Extraction(message_id="m", items=[item()]), "extracted")
    db.update("item", 1, "confirm", status="confirmed")
    db.set_reminders(1, plan(item(), "Asia/Singapore", "3d", 3, NOW)[0])  # 12 Oct, 14 Oct, 15 Oct 9am
    return db


def statuses(db):
    return [r["status"] for r in db.conn.execute("SELECT status FROM reminders ORDER BY remind_at")]


def test_sends_when_due_and_only_once(db):
    bot = FakeBot()
    assert send_due(db, bot, "Asia/Singapore", datetime(2026, 10, 12, 8, 59, tzinfo=SG))["sent"] == 0
    assert send_due(db, bot, "Asia/Singapore", datetime(2026, 10, 12, 9, 0, tzinfo=SG))["sent"] == 1
    assert send_due(db, bot, "Asia/Singapore", datetime(2026, 10, 12, 9, 1, tzinfo=SG))["sent"] == 0
    assert len(bot.sent) == 1 and "Due in 3 days" in bot.sent[0]
    assert statuses(db) == ["sent", "pending", "pending"]


def test_after_the_app_was_closed_only_the_latest_goes_out(db):
    bot = FakeBot()
    result = send_due(db, bot, "Asia/Singapore", datetime(2026, 10, 14, 20, 0, tzinfo=SG))
    assert (result["sent"], result["missed"]) == (1, 1)
    assert statuses(db) == ["missed", "sent", "pending"]


def test_nothing_is_sent_once_the_day_is_over(db):
    bot = FakeBot()
    send_due(db, bot, "Asia/Singapore", datetime(2026, 10, 16, 8, 0, tzinfo=SG))
    assert bot.sent == [] and statuses(db) == ["missed", "missed", "missed"]


def test_undone_items_are_cancelled_not_sent(db):
    db.undo(1)  # back to proposed
    bot = FakeBot()
    assert send_due(db, bot, "Asia/Singapore", datetime(2026, 10, 12, 9, 0, tzinfo=SG))["cancelled"] == 1
    assert bot.sent == []


def test_failures_stay_pending_and_are_said_once(db):
    bot = FakeBot(fail="Forbidden: bot was blocked by the user")
    job = ReminderJob(db, "Asia/Singapore", lambda: bot)
    job.tick(datetime(2026, 10, 12, 9, 0, tzinfo=SG))
    job.tick(datetime(2026, 10, 12, 9, 1, tzinfo=SG))
    assert statuses(db)[0] == "pending"
    said = [m["text"] for m in db.messages("calendar")]
    assert len(said) == 1 and "bot was blocked" in said[0]
    bot.fail = None
    assert job.tick(datetime(2026, 10, 12, 9, 2, tzinfo=SG))["sent"] == 1


def test_without_telegram_it_waits_and_says_how_to_connect(db):
    job = ReminderJob(db, "Asia/Singapore", lambda: None)
    assert job.tick(datetime(2026, 10, 12, 9, 0, tzinfo=SG))["waiting"] == 1
    assert statuses(db)[0] == "pending"
    assert "python -m doraemon.telegram connect" in db.messages("calendar")[-1]["text"]


# --- through the web app -------------------------------------------------------------

@pytest.fixture
def web(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone="Asia/Singapore",
                            google_token=str(tmp_path / "token.json"),
                            google_credentials=str(tmp_path / "credentials.json"))
    db = Database(s.db_path)
    day = datetime.now(SG).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=5)
    email = ParsedEmail(message_id="m", subject="Bill", sender="SP <bill@sp.com.sg>", sent_at=NOW, body="")
    db.save_result("g1", "t1", email, Extraction(message_id="m", items=[item(start=day)]), "extracted")

    def make(bot=None):
        client = TestClient(create_app(s, messenger=bot), follow_redirects=True)
        client.headers["x-requested-with"] = "fetch"
        return client, db
    return make


def test_card_offers_remind_me_instead_of_confirm(web):
    client, _ = web()
    page = client.get("/pocket").text
    assert 'action="/items/1/remind"' in page and 'action="/items/1/confirm"' not in page
    assert '<option value="1d" selected>the day before</option>' in page
    assert '<option value="3h"' not in page  # all-day item: whole days only


def test_remind_me_confirms_and_plans_reminders(web):
    client, db = web(FakeBot())
    body = client.post("/items/1/remind", data={"first": "3d", "times": "3"}).json()
    assert body["remove"] and body["mood"] == "happy"
    assert "on Telegram 3 times" in body["html"] and "3 days before" in body["html"]
    assert db.get("item", 1)["status"] == "confirmed" and db.get("item", 1)["calendar_event_id"] is None
    assert [r["label"] for r in db.reminders(1)] == ["3 days before", "the day before", "that morning"]
    upcoming = client.get("/upcoming").text
    assert "🔔 " in upcoming and "Change reminders" in upcoming and "/items/1/sync" not in upcoming


def test_reminders_that_would_all_have_passed_dont_confirm(web):
    client, db = web(FakeBot())
    body = client.post("/items/1/remind", data={"first": "1w", "times": "1"}).json()  # due in 5 days
    assert "already passed" in body["html"] and not body["remove"]
    assert db.get("item", 1)["status"] == "proposed" and db.reminders(1) == []


def test_bot_says_the_reminder_was_created_then_updated(web):
    bot = FakeBot()
    client, _ = web(bot)
    client.post("/items/1/remind", data={"first": "1d", "times": "2"})
    lines = bot.sent[0].splitlines()
    assert lines[0] == "✅ Reminder created: Electricity bill" and lines[1].startswith("Due in 5 days")
    assert lines[3:] == ["I'll remind you:", lines[4], lines[5]] and "(the day before)" in lines[4]
    assert "9am (that morning)" in lines[5]
    client.post("/items/1/remind", data={"first": "0", "times": "1", "back": "/upcoming"})
    assert bot.sent[1].startswith("✏️ Reminders updated: Electricity bill") and len(bot.sent) == 2


def test_reminders_are_kept_when_the_confirmation_cant_be_sent(web):
    client, db = web(FakeBot(fail="Forbidden: bot was blocked by the user"))
    html = client.post("/items/1/remind", data={"first": "1d", "times": "2"}).json()["html"]
    assert "couldn" in html and "bot was blocked" in html and "still set" in html
    assert db.get("item", 1)["status"] == "confirmed" and len(db.reminders(1)) == 2


def test_changing_reminders_replaces_the_unsent_ones(web):
    client, db = web(FakeBot())
    client.post("/items/1/remind", data={"first": "3d", "times": "3"})
    body = client.post("/items/1/remind", data={"first": "0", "times": "1", "back": "/upcoming"}).json()
    assert not body["remove"]  # already confirmed: just changed
    assert [r["label"] for r in db.reminders(1)] == ["that morning"]
    assert len(db.recent_actions()) == 1  # one confirm in the activity log, not two


def test_undo_cancels_the_reminders(web):
    client, db = web(FakeBot())
    client.post("/items/1/remind", data={"first": "1d", "times": "2"})
    client.post("/undo/1")
    assert db.get("item", 1)["status"] == "proposed" and db.reminders(1) == []
    assert "cancelled its reminders" in db.messages("calendar")[-1]["text"]


def test_plain_confirm_on_a_bill_sets_default_reminders(web):
    client, db = web(FakeBot())
    client.post("/items/1/confirm")
    assert [r["label"] for r in db.reminders(1)] == ["the day before", "that morning"]


def test_says_to_connect_telegram_when_it_isnt(web):
    client, db = web(None)
    html = client.post("/items/1/remind", data={"first": "1d", "times": "1"}).json()["html"]
    assert "python -m doraemon.telegram connect" in html
    assert len(db.reminders(1)) == 1  # kept, and sent once you connect


# --- the Telegram client ---------------------------------------------------------------

def test_send_posts_plain_text_to_your_chat():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})
    bot = Telegram("123:SECRET", "42", httpx.Client(transport=httpx.MockTransport(handler)))
    bot.send("🔔 Electricity bill")
    path, body = seen[0]
    assert path == "/bot123:SECRET/sendMessage"
    assert body["chat_id"] == "42" and body["text"] == "🔔 Electricity bill" and "parse_mode" not in body


def test_errors_never_include_the_token():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    bot = Telegram("123:SECRET", "42", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(TelegramError) as e:
        bot.send("hi")
    assert str(e.value) == "Unauthorized"

    def broken(request):
        raise httpx.ConnectError(f"failed to connect to {request.url}")
    bot = Telegram("123:SECRET", "42", httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(TelegramError) as e:
        bot.send("hi")
    assert "SECRET" not in str(e.value)


# --- /reminders on the bot ------------------------------------------------------------

def test_list_shows_each_item_with_its_reminder_times(db):
    text = list_text(db, "Asia/Singapore", NOW)
    assert text.splitlines() == [
        "🔔 Your reminders (1 item)", "",
        "Electricity bill", "Due in 5 days, Thu 15 Oct",
        "• Mon 12 Oct, 9am (3 days before)", "• Wed 14 Oct, 9am (the day before)", "• Thu 15 Oct, 9am (that morning)",
    ]


def test_list_leaves_out_sent_and_undone_reminders(db):
    send_due(db, FakeBot(), "Asia/Singapore", datetime(2026, 10, 12, 9, 0, tzinfo=SG))
    assert "Mon 12 Oct" not in list_text(db, "Asia/Singapore", NOW)
    db.undo(1)
    assert list_text(db, "Asia/Singapore", NOW).startswith("No reminders set")


class FakeChatBot(FakeBot):
    chat_id = "42"

    def __init__(self, messages):
        super().__init__()
        self.inbox, self.menu = messages, None

    def set_commands(self, commands):
        self.menu = commands

    def updates(self, offset, timeout):
        found = [u for u in self.inbox if offset is None or u["update_id"] >= offset]
        return found


def update(n, text, chat="42", age=0):
    return {"update_id": n, "message": {"chat": {"id": int(chat)}, "text": text, "date": int(time.time()) - age}}


def listener(db, bot):
    return CommandListener(db, lambda: bot, [
        Command("reminders", "List your reminders", lambda: list_text(db, "Asia/Singapore", NOW), ("remindars",)),
        Command("help", "What I can do", lambda: "help", ("start",)),
    ])


def test_bot_answers_reminders_and_its_misspelling(db):
    bot = FakeChatBot([update(1, "/reminders"), update(2, "/remindars@dorae_agent_bot")])
    assert listener(db, bot).poll_once() == 2
    assert all(m.startswith("🔔 Your reminders") for m in bot.sent)
    assert bot.menu == [("reminders", "List your reminders"), ("help", "What I can do")]


def test_bot_answers_each_message_once(db):
    bot = FakeChatBot([update(1, "/reminders")])
    listen = listener(db, bot)
    listen.poll_once()
    listen.poll_once()
    assert len(bot.sent) == 1


def test_strangers_and_old_commands_get_no_answer(db):
    bot = FakeChatBot([update(1, "/reminders", chat="999"), update(2, "/reminders", age=3600)])
    assert listener(db, bot).poll_once() == 0 and bot.sent == []


def test_anything_else_gets_the_command_list(db):
    bot = FakeChatBot([update(1, "hello")])
    lst = listener(db, bot)
    lst.poll_once()
    assert bot.sent == [lst.help_text()] and "/reminders: List your reminders" in bot.sent[0]


# --- asking Dorae-2 for a reminder -------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("create me a reminder to go to get groceries tmr", ("Go to get groceries", "tomorrow")),
    ("remind me to call mom at 5pm tomorrow", ("Call mom", "at 5pm tomorrow")),
    ("Remind me to pay rent on 1/11 9am pls", ("Pay rent", "on 1/11 9am")),
    ("set a reminder for dentist next monday at 9am", ("Dentist", "next monday at 9am")),
    ("can you remind me to take out the trash in 2 hours", ("Take out the trash", "in 2 hours")),
    ("remind me to buy milk", ("Buy milk", "")),
    ("what are my reminders?", None),
    ("show my schedule this week", None),
])
def test_reminder_requests_split_into_title_and_when(text, expected):
    assert split_request(text) == expected
