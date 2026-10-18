"""The fast path: everyday questions answered from the data, without the model."""
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.router import route
from doraemon.schema import ActionItem, Extraction, Transaction
from doraemon.web import create_app

NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize("agent, question, intent, slots", [
    ("money", "How much did I spend on dining last month?", "total", {"category": "dining", "period": "last_month"}),
    ("money", "how much did i spend today", "total", {"period": "today"}),
    ("money", "how much i have spent on dining this month", "total", {"category": "dining", "period": "this_month"}),
    ("money", "how much have i spent on September", "total", {"period": "month:9"}),
    ("money", "how much I've spent this week", "total", {"period": "this_week"}),
    ("money", "how much i spent yesterday", "total", {"period": "yesterday"}),
    ("money", "how much spent on groceries last month", "total", {"category": "groceries", "period": "last_month"}),
    ("money", "hey dorae-1 how much have I spent this week please", "total", {"period": "this_week"}),
    ("money", "how much have I spent at grab?", "total", {"merchant": "grab"}),
    ("money", "what did I spend in sept", "total", {"period": "month:9"}),
    ("money", "my food spending this month", "total", {"category": "dining", "period": "this_month"}),
    ("money", "Show this month's breakdown", "breakdown", {"period": "this_month"}),
    ("money", "spending by category last month", "breakdown", {"period": "last_month"}),
    ("money", "my spending breakdown this month", "breakdown", {"period": "this_month"}),
    ("money", "What's my latest purchase?", "latest", {}),
    ("money", "recent payments", "latest", {}),
    ("money", "what was the biggest thing I bought last month", "biggest", {"period": "last_month"}),
    ("money", "Where did I spend the most?", "top", {}),
    ("money", "show my groceries payments", "payments", {"category": "groceries"}),
    ("money", "How was September?", "how_was", {"period": "month:9"}),
    ("money", "how am I doing on my budget?", "budget", {}),
    ("money", "Is my spending going up or down?", "trend", {}),
    ("money", "dining spending by month", "trend", {"category": "dining"}),
    ("calendar", "What needs my OK?", "waiting", {}),
    ("calendar", "Show my schedule this week", "week", {"which": "this"}),
    ("calendar", "what's on next week", "week", {"which": "next"}),
    ("calendar", "can you check what do i have going on this week", "week", {"which": "this"}),
    ("calendar", "what about next week", "week", {"which": "next"}),
    ("calendar", "any events this week", "week", {"which": "this"}),
    ("calendar", "show me next week", "week", {"which": "next"}),
    ("calendar", "how's my week looking", "week", {}),
    ("calendar", "am i busy next week", "week", {"which": "next"}),
    ("calendar", "what's planned for next week", "week", {"which": "next"}),
    ("calendar", "what's on tmr", "day", {"day": "tomorrow"}),
    ("calendar", "anything due today?", "day", {"day": "today"}),
    ("calendar", "what's due this week?", "due", {}),
    ("calendar", "My upcoming trips", "trips", {}),
    ("calendar", "what are my reminders", "reminders", {}),
    ("calendar", "what already passed?", "passed", {}),
])
def test_everyday_questions_take_the_fast_path(agent, question, intent, slots):
    found = route(agent, question)
    assert (found.agent, found.intent, found.slots) == (agent, intent, slots)


@pytest.mark.parametrize("agent, question", [
    ("money", "how can I stay within my budget?"),              # advice: the model gives tips
    ("money", "why is my dining spending so high"),
    ("money", "how much did I spend on dining compared to last month?"),
    ("money", "should I cancel any subscriptions"),
    ("money", "set my dining budget to 300"),                  # its own fast path (a card to Save)
    ("money", "what did I buy at the airport on my trip"),
    ("calendar", "when should I book the dentist?"),            # advice: the model's
    ("calendar", "any bills due this month?"),
    ("calendar", "add dinner with mum next friday 7pm"),
    ("calendar", "what's the capital of France?"),
])
def test_anything_else_goes_to_the_model(agent, question):
    assert route(agent, question) is None


def test_questions_for_the_other_agent_are_recognised():
    assert route("calendar", "how much did I spend today").agent == "money"
    assert route("money", "what needs my ok").agent == "calendar"


def test_follow_ups_reuse_the_last_question():
    found = route("money", "and last month?", previous="how much did I spend on dining this month")
    assert (found.intent, found.slots) == ("total", {"category": "dining", "period": "last_month"})
    assert route("money", "what about september", previous="where did I spend the most").slots == {"period": "month:9"}
    assert route("money", "and last month?", previous="why is it so high") is None
    assert route("money", "and last month?") is None
    # a money follow-up wins over Dorae-2's reading of "this week"
    assert route("money", "and this week?", previous="how much did I spend last week").slots == {"period": "this_week"}
    assert route("money", "and this week?").agent == "calendar"


def test_week_questions_that_need_the_model_dont_take_the_shortcut():
    for q in ("why is next week so busy", "add dinner next week", "remind me next week to call mom",
              "is next week a public holiday", "what about last week"):
        assert route("calendar", q) is None, q


# --- the answers, with a model that must never be called ------------------------------------

class NoModel:
    def complete_json(self, *a, **k):
        raise AssertionError("the model was used")

    chat = complete_json


def pay(db, n, merchant, amount, category, when=None):
    txn = Transaction(message_id="m", source="bank_alert", merchant=merchant, order_ref=None,
                      purchased_at=when or NOW, amount=Decimal(amount), currency="SGD", amount_home=None,
                      category=category, is_refund=False)
    email = ParsedEmail(message_id="m", subject="s", sender="Bank <a@bank.sg>", sent_at=NOW, body="")
    db.save_result(f"p{n}", "t", email, Extraction(message_id="m", transactions=[txn]), "extracted")


@pytest.fixture
def ask(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), home_currency="SGD", timezone="Asia/Singapore",
                            google_token=str(tmp_path / "t.json"), google_credentials=str(tmp_path / "c.json"))
    db = Database(s.db_path)
    pay(db, 1, "Kopitiam", "7.02", "dining")
    pay(db, 2, "Uniqlo", "59.90", "shopping")
    pay(db, 3, "Grab", "12.40", "transport")
    app = create_app(s, chat_backend=NoModel())
    client = TestClient(app, headers={"x-requested-with": "fetch"})

    def go(agent, question):
        html = client.post(f"/chat/{agent}/ask", data={"q": question}).json()["html"]
        assert "the local model isn" not in html  # never fell back because the model failed
        return html
    go.db, go.brain = db, app.state.runtime.brain
    return go


def test_spending_today_without_the_model(ask):
    html = ask("money", "how much did I spend today?")
    assert "You spent SGD 79.32 today" in html and "across 3 payments" in html and 'class="viz donut"' in html
    assert ask.brain.last_route == "total"


def test_category_and_merchant_show_the_payments(ask):
    html = ask("money", "how much did I spend on food this month")
    assert "You spent SGD 7.02 on dining" in html and "Kopitiam" in html and '<table class="payments">' in html
    assert "You spent SGD 12.40 at Grab" in ask("money", "how much have I spent at grab?")


def test_latest_and_biggest(ask):
    assert "Your biggest purchase: Uniqlo, SGD 59.90" in ask("money", "what's my biggest purchase")
    assert "Your latest purchase:" in ask("money", "what's my latest purchase?")


def test_follow_up_in_the_chat(ask):
    ask("money", "how much did I spend on dining this month")
    html = ask("money", "and last month?")  # all the test's payments are from now
    assert "No payments on dining in " in html and ask.brain.last_route == "total"


def test_calendar_lookups_and_pointing_to_the_other_agent(ask):
    assert "Nothing needs your OK right now" in ask("calendar", "what needs my ok?")
    assert "No reminders set" in ask("calendar", "what are my reminders")
    assert "That&#39;s one for Dorae-1" in ask("calendar", "how much did I spend today")


def test_reminders_list_comes_from_telegram_reminders(ask):
    item = ActionItem(message_id="m", type="bill", title="SP electricity", start_at=NOW + timedelta(days=3),
                      end_at=None, all_day=True, timezone="Asia/Singapore", evidence_snippet="", confidence=0.9)
    email = ParsedEmail(message_id="m", subject="s", sender="SP <a@sp.sg>", sent_at=NOW, body="")
    ask.db.save_result("b1", "t", email, Extraction(message_id="m", items=[item]), "extracted")
    ask.db.update("item", 1, "confirm", status="confirmed")
    ask.db.set_reminders(1, [{"at": NOW + timedelta(days=2), "label": "1 day before"}])
    html = ask("calendar", "my reminders")
    assert "1 thing with reminders on Telegram. Next: “SP electricity”" in html


def test_off_sends_everything_to_the_model(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), fast_answers=False,
                            google_token=str(tmp_path / "t.json"), google_credentials=str(tmp_path / "c.json"))
    client = TestClient(create_app(s, chat_backend=NoModel()), headers={"x-requested-with": "fetch"})
    html = client.post("/chat/money/ask", data={"q": "how much did I spend today"}).json()["html"]
    assert "the local model isn" in html  # it tried the model (which "failed" here), then the fixed answer


def test_instant_answers_get_a_bolt_and_model_answers_dont(ask, tmp_path, settings):
    assert 'class="instant"' in ask("money", "how much did I spend today?")
    assert ask.db.last_message("money")["via"] == "total"
    s = dataclasses.replace(settings, db_path=str(tmp_path / "e.db"), fast_answers=False,
                            google_token=str(tmp_path / "t.json"), google_credentials=str(tmp_path / "c.json"))
    client = TestClient(create_app(s, chat_backend=NoModel()), headers={"x-requested-with": "fetch"})
    html = client.post("/chat/money/ask", data={"q": "how much did I spend today"}).json()["html"]
    assert 'class="instant"' not in html  # it went the model's way


# --- the looser reading (understand): topic and time words anywhere, typos fixed ------------------

@pytest.mark.parametrize("agent, question, intent, slots", [
    ("calendar", "show me my reminders", "reminders", {}),
    ("calendar", "do i have any reminders", "reminders", {}),
    ("calendar", "whats my schedule like next week", "week", {"which": "next"}),
    ("calendar", "do i have anything on november", "days", {"when": "month:11"}),
    ("calendar", "am i free on saturday", "days", {"when": "weekday:5"}),
    ("calendar", "what's on wednesday", "days", {"when": "weekday:2"}),
    ("calendar", "anything this weekend?", "days", {"when": "this_weekend"}),
    ("calendar", "any appointments tomorrow", "days", {"when": "tomorrow"}),
    ("calendar", "what's happening on 26/10", "days", {"when": "date:26/10"}),
    ("calendar", "when is my next flight", "trips", {}),
    ("calendar", "any bills i need to pay", "due", {}),
    ("money", "what did i spend on today", "payments", {"period": "today"}),
    ("money", "did i have a 4.87 transaction today?", "payments", {"period": "today", "amount": "4.87"}),
    ("money", "how much have i spent this monrh", "total", {"period": "this_month"}),  # a typo
    ("money", "spemt on food this week", "total", {"period": "this_week", "category": "dining"}),
    ("money", "how much did grab cost me this month", "total", {"period": "this_month", "merchant": "grab"}),
    ("money", "how much did i spend on netflix", "total", {"merchant": "netflix"}),
    ("money", "what have i bought recently", "latest", {}),
    ("money", "what's my biggest expense this month", "biggest", {"period": "this_month"}),
    ("money", "how am i tracking against my budget", "budget", {}),
    ("money", "what can dorae-1 do", "help", {}),
    ("calendar", "when is my dentist appointment?", "find", {"what": "dentist appointment"}),
    ("calendar", "what time is my flight to tokyo", "find", {"what": "flight to tokyo"}),
    ("calendar", "when is my next flight", "trips", {}),  # the list, not a search
    ("money", "monthly summary", "summary", {}),
    ("money", "september in review", "summary", {"period": "month:9"}),
    ("money", "give me a recap of last month", "summary", {"period": "last_month"}),
])
def test_freely_worded_lookups_skip_the_model(agent, question, intent, slots):
    found = route(agent, question)
    assert (found.intent, found.slots) == (intent, slots)


@pytest.mark.parametrize("agent, question", [
    ("money", "why did i spend so much this month"),
    ("money", "how can i cut down on my spendings?"),
    ("money", "should i set a budget for dining"),
    ("money", "compare this month to last month"),
    ("money", "can you set my budget next month to be 400"),
    ("calendar", "remind me about the bill"),  # "remind" isn't a typo of "reminders"
    ("calendar", "add something to my calendar"),
    ("calendar", "cancel my dentist appointment"),
    ("calendar", "when should i pay my bill"),
    ("calendar", "any bills due this month?"),
    ("money", "what did I buy at the airport on my trip"),
    ("money", "tell me a joke about money"),
])
def test_questions_that_need_thought_or_change_things_still_go_to_the_model(agent, question):
    assert route(agent, question) is None


def test_a_bare_follow_up_of_a_calendar_question():
    assert route("calendar", "what about friday", previous="what do i have this week").slots == {"when": "weekday:4"}
    assert route("calendar", "what about friday") is None  # without one, it could be about anything
