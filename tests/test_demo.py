"""The demo (python -m doraemon.demo): made-up data only, and no way out to anything real."""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from doraemon.demo import TZ, DemoCalendar, DemoSchedule, demo_settings, no_mail, seed
from doraemon.web import create_app


def demo(tmp_path):
    settings = demo_settings(tmp_path)
    today = datetime.now(ZoneInfo(TZ)).date()
    app = create_app(settings, schedule=DemoSchedule(today), calendar=DemoCalendar(), ingest=no_mail)
    seed(app.state.runtime, today)
    return settings, app


def test_the_demo_connects_to_nothing_real(tmp_path):
    s, app = demo(tmp_path)
    assert (s.telegram_token, s.telegram_chat_id, s.webull_app_key, s.chat_model, s.poll_minutes) == ("", "", "", "off", 0)
    assert s.db_path.startswith(str(tmp_path)) and s.google_token.startswith(str(tmp_path))
    rt = app.state.runtime
    assert rt.get_messenger() is None and rt.brokers is None and rt.webull_expires() is None
    assert rt.check.ready() and rt.check.ingest is no_mail  # "Check now" reads no inbox


def test_every_page_shows_made_up_data(tmp_path):
    _, app = demo(tmp_path)
    client = TestClient(app, follow_redirects=True)
    calendar = client.get("/chat/calendar").text
    assert "SP Group electricity bill" in calendar and "Team stand-up" in calendar and "Good " in calendar  # briefing
    assert "You spent SGD" in client.get("/chat/money").text
    for page in ("/spending", "/budgets", "/networth", "/income", "/upcoming", "/history", "/rules", "/pocket"):
        assert client.get(page).status_code == 200, page
    assert "DBS Multiplier" in client.get("/networth").text
