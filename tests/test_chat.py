"""The agents' chats with a scripted model: guardrails, tools and cards, no Ollama needed."""
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.schema import ActionItem, Extraction, Transaction
from doraemon.web import create_app

NOW = datetime.now(timezone.utc)


class ScriptedModel:
    """Answers the topic check with `topic`, then plays back `turns` (tool calls or a final answer)."""

    def __init__(self, topic, *turns):
        self.topic, self.turns, self.seen = topic, list(turns), []

    def complete_json(self, system, user, schema):
        if isinstance(self.topic, Exception):
            raise self.topic
        return json.dumps({"topic": self.topic})

    def chat(self, messages, tools):
        self.seen.append({"messages": [dict(m) for m in messages], "tools": [t["function"]["name"] for t in tools]})
        return self.turns.pop(0)


def call(name, **arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def say(text):
    return {"role": "assistant", "content": text}


def email():
    return ParsedEmail(message_id="m", subject="s", sender="Bank <a@bank.sg>", sent_at=NOW, body="")


@pytest.fixture
def chat(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), home_currency="SGD",
                            google_token=str(tmp_path / "t.json"), google_credentials=str(tmp_path / "c.json"))
    db = Database(s.db_path)
    for i, (merchant, amount, days_ago) in enumerate([("Kopitiam", "7.02", 3), ("Uniqlo", "59.90", 1)], 1):
        txn = Transaction(message_id="m", source="bank_alert", merchant=merchant, order_ref=None,
                          purchased_at=NOW - timedelta(days=days_ago), amount=Decimal(amount), currency="SGD",
                          amount_home=None, category="shopping" if merchant == "Uniqlo" else "dining", is_refund=False)
        db.save_result(f"g{i}", f"t{i}", email(), Extraction(message_id="m", transactions=[txn]), "extracted")
    dentist = ActionItem(message_id="m", type="appointment", title="Dentist check-up", start_at=NOW + timedelta(days=4),
                         end_at=None, all_day=False, timezone=s.timezone, evidence_snippet="", confidence=0.9)
    db.save_result("g9", "t9", email(), Extraction(message_id="m", items=[dentist]), "extracted")

    def ask(agent, question, model):
        client = TestClient(create_app(s, chat_backend=model), headers={"x-requested-with": "fetch"})
        return client.post(f"/chat/{agent}/ask", data={"q": question}).json()["html"]
    return ask


def test_latest_purchase_comes_from_the_database(chat):
    model = ScriptedModel("spending", call("list_payments", limit=1),
                          say("Your latest purchase was **SGD 59.90** at Uniqlo."))
    html = chat("money", "what's my latest purchase?", model)
    assert "Your latest purchase was SGD 59.90 at Uniqlo." in html  # markdown stripped
    assert '<table class="payments">' in html and "Uniqlo" in html.split("payments")[1]  # the card
    tool_result = json.loads(model.seen[1]["messages"][-1]["content"])
    assert tool_result["payments"][0]["merchant"] == "Uniqlo" and tool_result["found"] == 2


def test_each_agent_only_has_its_own_tools(chat):
    money = ScriptedModel("spending", call("find_items", text="dentist"), say("I can't see that."))
    chat("money", "how much did I spend?", money)
    assert money.seen[0]["tools"] == ["list_payments", "spending_summary", "top_merchants"]
    assert "no tool called 'find_items'" in money.seen[1]["messages"][-1]["content"]  # nothing leaked
    calendar = ScriptedModel("calendar", call("find_items", text="dentist"), say("Your dentist is on Friday."))
    html = chat("calendar", "when's my dentist?", calendar)
    assert calendar.seen[0]["tools"] == ["find_items", "week_schedule"]
    assert "Dentist check-up" in html and "Your dentist is on Friday." in html


@pytest.mark.parametrize("agent,topic,expected", [
    ("money", "calendar", "That&#39;s one for Dorae-2"),
    ("calendar", "spending", "That&#39;s one for Dorae-1"),
    ("money", "other", "Sorry, I can only help with your payments"),
    ("calendar", "hello", "Hi! I&#39;m Dorae-2"),
])
def test_off_topic_questions_never_reach_the_tools(chat, agent, topic, expected):
    model = ScriptedModel(topic)  # no turns: calling the chat model would fail the test
    assert expected in chat(agent, "anything", model)
    assert model.seen == []


def test_unreadable_topic_is_treated_as_off_topic(chat):
    model = ScriptedModel(ValueError("not json"))
    assert "Sorry, I can only help" in chat("money", "hm", model) and model.seen == []


def test_bad_arguments_are_cleaned_up(chat):
    model = ScriptedModel("spending", call("list_payments", month="last month", limit="lots", category="caviar"),
                          say("Two payments."))
    chat("money", "list my payments", model)
    result = json.loads(model.seen[1]["messages"][-1]["content"])
    assert result["found"] == 2 and result["showing"] == 2  # bad month/category ignored, limit defaulted


def test_model_must_answer_after_a_few_tool_rounds(chat):
    model = ScriptedModel("spending", *[call("spending_summary")] * 4, say("SGD 66.92 this month."))
    assert "SGD 66.92 this month." in chat("money", "how much this month?", model)
    assert model.seen[-1]["tools"] == []  # last turn offers no tools


def test_model_not_running_falls_back_to_fixed_answers(chat):
    model = ScriptedModel(httpx.ConnectError("refused"))
    html = chat("money", "show this month's breakdown", model)
    assert "by category" in html and "the local model isn" in html


def test_follow_ups_get_earlier_messages(chat, tmp_path):
    first = ScriptedModel("spending", call("spending_summary"), say("SGD 66.92 in total."))
    chat("money", "how much this month?", first)
    second = ScriptedModel("spending", call("spending_summary"), say("Less."))
    chat("money", "and last month?", second)
    roles = [(m["role"], m["content"]) for m in second.seen[0]["messages"][1:]]
    assert ("user", "how much this month?") in roles and ("assistant", "SGD 66.92 in total.") in roles


def test_find_items_forgives_small_model_habits(chat):
    # "when's my dentist?" with the type repeated as search text and only an end date
    model = ScriptedModel("calendar", call("find_items", text="appointments", type="appointment",
                                           to_date=(NOW + timedelta(days=30)).date().isoformat()), say("Friday."))
    chat("calendar", "any appointments this month?", model)
    result = json.loads(model.seen[1]["messages"][-1]["content"])
    assert [i["title"] for i in result["items"]] == ["Dentist check-up"]
