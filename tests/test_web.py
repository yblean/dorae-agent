import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from doraemon.db import Database
from doraemon.email_parse import ParsedEmail
from doraemon.schema import ActionItem, Extraction, Transaction
from doraemon.web import create_app

SOON = datetime.now(timezone.utc) + timedelta(days=5)


def email(sender="Trust <from_us@trustbank.sg>", subject="Hello"):
    return ParsedEmail(message_id="m", subject=subject, sender=sender, sent_at=datetime.now(timezone.utc), body="")


def txn(merchant, amount, category="other"):
    return Transaction(message_id="m", source="bank_alert", merchant=merchant, order_ref=None,
                       purchased_at=SOON - timedelta(days=10), amount=Decimal(amount), currency="SGD",
                       amount_home=None, category=category, is_refund=False)


@pytest.fixture
def client(tmp_path, settings):
    s = dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), home_currency="SGD",
                            google_token=str(tmp_path / "token.json"),
                            google_credentials=str(tmp_path / "credentials.json"))
    db = Database(s.db_path)
    item = ActionItem(message_id="m", type="bill", title="SIT tuition fees", start_at=SOON, end_at=None,
                      all_day=True, timezone=s.timezone, evidence_snippet="<script>alert(1)</script> due 27 Oct",
                      confidence=0.9)
    db.save_result("g1", "t1", email(subject="Fee Statement"), Extraction(message_id="m", items=[item]), "extracted")
    db.save_result("g2", "t2", email(), Extraction(message_id="m", transactions=[txn("Kopitiam Investment Pte L", "7.02", "groceries")]), "extracted")
    db.save_result("g3", "t3", email(), Extraction(message_id="m", transactions=[txn("KOPITIAM INVESTMENT PTE LSINGAPORE SG", "7.92", "groceries")]), "extracted")
    return TestClient(create_app(s), follow_redirects=True)


def test_calendar_agent_opens_with_overview_and_escapes_email_text(client):
    page = client.get("/").text  # lands on Dorae-2
    assert "Dorae-2" in page and "need your OK" in page
    assert "SIT tuition fees" in page
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_overview_is_posted_once_a_day(client):
    client.get("/chat/calendar")
    client.get("/chat/calendar")
    thread = client.get("/chat/calendar").text.split('id="thread"')[1]
    assert thread.count("need your OK.") == 1


def test_confirm_then_undo(client):
    client.post("/items/1/confirm")
    assert "Nothing waiting for your OK" in client.get("/pocket").text
    assert "confirm" in client.get("/history").text
    client.post("/undo/1")
    assert "SIT tuition fees" in client.get("/pocket").text


def test_buttons_answer_in_the_agents_chat(client):
    body = client.post("/items/1/confirm", headers={"x-requested-with": "fetch"}).json()
    assert "SIT tuition fees" in body["html"] and body["remove"] and body["mood"] == "happy"
    assert "remind you about “SIT tuition fees” on Telegram" in client.get("/chat/calendar").text  # kept in Dorae-2's history


def test_category_change_creates_rule_and_fixes_other_payments(client):
    page = client.post("/transactions/1/category", data={"category": "dining"}).text
    assert "1 other payment" in page  # Dorae-1 says it in its chat
    spending = client.get("/spending").text
    assert spending.count('<option value="dining" selected>') == 2
    assert "kopitiam investment" in client.get("/rules").text


def test_agents_answer_from_your_data(client):
    client.headers["x-requested-with"] = "fetch"  # as the page's script sends it
    html = client.post("/chat/calendar/ask", data={"q": "What's due this week?"}).json()["html"]
    assert "SIT tuition fees" in html and "What&#39;s due this week?" in html
    month = (SOON - timedelta(days=10)).strftime("%B")
    html = client.post("/chat/money/ask", data={"q": f"How was {month}?"}).json()["html"]
    assert "SGD 14.94" in html  # 7.02 + 7.92
    html = client.post("/chat/money/ask", data={"q": f"Show {month} groceries payments"}).json()["html"]
    assert "Kopitiam Investment Pte L" in html and 'data-autosubmit' in html
    assert "I look after your spending" in client.post("/chat/money/ask", data={"q": "tell me a joke"}).json()["html"]


def test_new_chat_clears_history_and_starts_with_fresh_overview(client):
    client.post("/chat/money/ask", data={"q": "tell me a joke"})
    page = client.post("/chat/money/clear").text
    assert "tell me a joke" not in page
    assert "Ask me for any month" in page


def test_sidebar_marks_unread_agents(client):
    client.get("/chat/calendar")  # posts Dorae-2's overview and marks it read
    page = client.get("/agents").text
    assert page.count('aria-label="New messages"') == 0
    client.post("/chat/money/clear")  # Dorae-1 posts a new overview when opened...
    client.get("/chat/money")
    client.app.state.db.add_message("calendar", "agent", "Bill due tomorrow")
    assert client.get("/agents").text.count('aria-label="New messages"') == 1


def test_every_page_renders(client):
    for path in ["/", "/agents", "/chat/money", "/chat/calendar", "/pocket", "/pocket?show=past",
                 "/upcoming", "/spending", "/history", "/rules"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Dorae-1" in r.text and "Dorae-2" in r.text  # the agent list is on every page
    assert client.get("/chat/nobody").status_code == 404


def test_cross_site_post_is_blocked(client):
    r = client.post("/items/1/dismiss", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert "SIT tuition fees" in client.get("/pocket").text


def test_reprocessing_keeps_decisions(tmp_path, settings):
    db = Database(tmp_path / "d.db")
    item = ActionItem(message_id="m", type="deadline", title="Submit log", start_at=SOON, end_at=None,
                      all_day=True, timezone=settings.timezone, evidence_snippet="", confidence=0.9)
    db.save_result("g1", "t1", email(), Extraction(message_id="m", items=[item]), "extracted")
    db.update("item", 1, "confirm", status="confirmed")
    db.save_result("g1", "t1", email(), Extraction(message_id="m", items=[item]), "extracted")  # --again
    assert [r["status"] for r in db.conn.execute("SELECT status FROM action_items")] == ["confirmed", "proposed"]


def test_customize_agent_name_and_colour(client):
    page = client.get("/agents/money/customize").text
    assert page.count('name="color"') == 10 and 'value="#3B82F6" checked' in page
    page = client.post("/agents/money/customize", data={"name": "Penny", "color": "#22C55E"}).text
    assert "Penny" in page and "--agent: #22C55E" in page   # lands back in the chat, restyled
    assert "Penny" in client.get("/agents").text            # sidebar uses the new name
    # bad input falls back to the defaults instead of breaking the page
    client.post("/agents/money/customize", data={"name": "  ", "color": "red; background: url(x)"})
    page = client.get("/chat/money").text
    assert "Dorae-1" in page and "--agent: #3B82F6" in page


def test_time_separators_between_messages(client):
    page = client.get("/chat/calendar").text
    assert '<div class="sep">Today ' in page


def test_budget_question_is_answered_honestly(client):
    client.headers["x-requested-with"] = "fetch"
    html = client.post("/chat/money/ask", data={"q": "help me set a budget of $300 per month"}).json()["html"]
    assert "I can&#39;t save budgets yet" in html and "SGD 300.00" in html
    html = client.post("/chat/money/ask", data={"q": "set a budget"}).json()["html"]
    assert "Tell me an amount" in html


# --- item types ----------------------------------------------------------------------------

def edit(client, **form):
    data = {"title": "SIT tuition fees", "type": "bill", **form}
    return client.post("/items/1/edit", data=data, headers={"x-requested-with": "fetch"}).json()


def test_type_dropdown_has_readable_labels_grouped_by_where_items_go(client):
    page = client.get("/pocket").text
    assert '<optgroup label="Telegram reminder">' in page and '<optgroup label="Google Calendar event">' in page
    assert '<option value="other_travel" >Train, bus or ferry</option>' in page and "other travel" not in page
    assert '<option value="bill" selected>Bill</option>' in page and '<option value="__new__">' in page


def test_new_custom_type_is_saved_and_the_card_updates(client):
    body = edit(client, type="__new__", new_type="Study", new_base="appointment")
    assert "New type “Study” saved" in body["html"] and "Google Calendar event" in body["html"]
    assert ">Study</span>" in body["card"]  # the chip on the refreshed card
    assert 'action="/items/1/confirm"' in body["card"]  # works like a calendar event now: Confirm, not Remind me
    row = client.app.state.db.get("item", 1)
    assert row["type"] == "appointment" and row["custom_type_id"] == 1
    assert '<option value="custom:1" selected>Study</option>' in body["card"]


def test_custom_types_are_reused_by_name_and_builtins_win(client):
    edit(client, type="__new__", new_type="Study", new_base="reminder")
    body = edit(client, type="__new__", new_type="study", new_base="appointment")  # same name: no second type
    assert "New type" not in body["html"] and len(client.app.state.db.custom_types()) == 1
    assert client.app.state.db.get("item", 1)["type"] == "reminder"  # keeps how it first worked
    edit(client, type="__new__", new_type="Bill")
    assert client.app.state.db.get("item", 1)["type"] == "bill" and client.app.state.db.get("item", 1)["custom_type_id"] is None


def test_new_type_needs_a_name(client):
    assert "Give your new type a name" in edit(client, type="__new__", new_type="  ")["html"]


def test_deleting_a_type_keeps_its_items(client):
    edit(client, type="__new__", new_type="Study", new_base="reminder")
    assert "Study" in client.get("/rules").text
    client.post("/types/1/delete")
    row = client.app.state.db.get("item", 1)
    assert row["custom_type_id"] is None and row["type"] == "reminder"
    assert "None yet." in client.get("/rules").text


def test_chat_reminder_card_can_change_its_type(client):
    from doraemon.reminders import draft_reminder
    db = client.app.state.db
    _, item_id = draft_reminder(db, "America/New_York", "DMY", "LeetCode", "tomorrow 2pm", "remind me", SOON - timedelta(days=4))
    day = (SOON - timedelta(days=3)).date().isoformat()
    form = {"title": "LeetCode", "day": day, "at": "14:00", "first": "0", "times": "1"}
    body = client.post(f"/items/{item_id}/remind", data={**form, "type": "__new__", "new_type": "Study"},
                       headers={"x-requested-with": "fetch"}).json()
    assert "New type “Study” saved" in body["html"] and db.get("item", item_id)["status"] == "confirmed"
    assert db.get("item", item_id)["custom_type_id"] and len(db.reminders(item_id)) == 1


def test_chat_reminder_turned_into_a_calendar_event_waits_for_confirm(client):
    from doraemon.reminders import draft_reminder
    db = client.app.state.db
    _, item_id = draft_reminder(db, "America/New_York", "DMY", "Dinner", "tomorrow 7pm", "remind me", SOON - timedelta(days=4))
    body = client.post(f"/items/{item_id}/remind", data={"type": "appointment", "first": "0", "times": "1"},
                       headers={"x-requested-with": "fetch"}).json()
    assert "now a Google Calendar event" in body["html"] and f'action="/items/{item_id}/confirm"' in body["card"]
    assert db.get("item", item_id)["status"] == "proposed" and db.reminders(item_id) == []
