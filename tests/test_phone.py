"""Plain messages to the Telegram bot: reminders with Create/Cancel buttons, and instant answers."""
import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from doraemon.jobs import Runtime

TZ = "Asia/Singapore"


@pytest.fixture
def rt(tmp_path, settings):
    return Runtime(dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone=TZ, home_currency="SGD",
                                       poll_minutes=0, google_token=str(tmp_path / "t.json"),
                                       google_credentials=str(tmp_path / "c.json")))


def drafted(buttons):
    (make, make_data), (drop, drop_data) = buttons[0]
    assert (make, drop) == ("✅ Create", "✖ Cancel")
    return int(make_data.split(":")[1]), make_data, drop_data


def test_a_reminder_request_is_drafted_with_create_and_cancel(rt):
    text, buttons = rt.phone.answer("remind me to call mom at 5pm tomorrow")
    item_id, make, _ = drafted(buttons)
    assert text.startswith("📝 New reminder: Call mom\nTomorrow, ") and " at 5pm" in text
    assert "I'll remind you " in text and "(at the time)" in text
    assert rt.db.get("item", item_id)["status"] == "proposed"

    assert rt.button(make).startswith("✅ Created. I'll remind you ")
    assert rt.db.get("item", item_id)["status"] == "confirmed" and len(rt.db.reminders(item_id)) == 1
    assert rt.db.last_message("calendar")["text"].startswith("🔔 Reminder created on Telegram: “Call mom”")
    assert rt.button(make) == "✅ Already created."
    assert rt.phone.answer("remind me to call mom at 5pm tomorrow")[0].startswith("You already have that reminder")


def test_cancel_dismisses_the_draft(rt):
    _, make, drop = drafted(rt.phone.answer("remind me to pay rent tomorrow")[1])
    assert rt.button(drop) == "✖ Cancelled."
    assert rt.button(make).startswith("That reminder was cancelled")


def test_without_a_date_it_asks_when_and_reads_the_answer(rt):
    text, buttons = rt.phone.answer("remind me to buy milk")
    assert buttons is None and text.startswith("When should I remind you to buy milk?")
    text, buttons = rt.phone.answer("tomorrow 6pm")
    assert text.startswith("📝 New reminder: Buy milk\nTomorrow, ") and " at 6pm" in text
    assert rt.phone.answer("tomorrow 6pm")[0] == rt.commands.help_text()  # only the very next message counts


def test_an_old_question_is_forgotten(rt):
    long_ago = datetime.now(ZoneInfo(TZ)) - timedelta(hours=1)
    rt.phone.answer("remind me to buy milk", now=long_ago)
    assert rt.phone.answer("tomorrow 6pm")[0] == rt.commands.help_text()


def test_everyday_questions_from_both_agents_are_answered_instantly(rt):
    assert rt.phone.answer("how much did I spend this week?") == ("No payments this week.", None) or \
        rt.phone.answer("how much did I spend this week?")[0].startswith("No payments")
    assert rt.phone.answer("and last month?")[0].startswith("No payments")  # a follow-up
    assert rt.phone.answer("what needs my OK?")[0] == "Nothing needs your OK right now. 🎉"
    _, make, _ = drafted(rt.phone.answer("remind me to call mom at 5pm tomorrow")[1])
    rt.button(make)
    text, _ = rt.phone.answer("my reminders")
    assert text.startswith("1 thing with reminders on Telegram. Next: “Call mom”") and "\n\n• Call mom · " in text


def test_anything_else_gets_examples_and_the_commands(rt):
    text, buttons = rt.phone.answer("why is the sky blue")
    assert buttons is None and text == rt.commands.help_text()
    assert "• remind me to call mom at 5pm tmr" in text and "/reminders: List your reminders" in text


def test_a_failure_is_said_not_lost(rt, monkeypatch):
    monkeypatch.setattr(rt.brain, "instant", lambda route: 1 / 0)
    assert rt.phone.answer("my budget")[0].startswith("Sorry, something went wrong")


def test_snooze_buttons_by_length(rt):
    rt.phone.answer("remind me to stretch tomorrow 5pm")
    item_id = rt.db.conn.execute("SELECT MAX(id) FROM action_items").fetchone()[0]
    rt.button(f"make:{item_id}")
    assert rt.button(f"snooze:{item_id}:tmr") == "⏰ Snoozed: I'll remind you again tomorrow at 9am."
    assert rt.button(f"snooze:{item_id}").startswith("⏰ Snoozed: I'll remind you again ")  # buttons sent before
    assert rt.button(f"snooze:{item_id}:soon") == "I don't know that button."

