"""Doraemon's web app: chat with Dorae-1 (spending) and Dorae-2 (calendar).

    python -m doraemon.web        # then open http://127.0.0.1:8000

Everything starts in an agent's chat: each posts a short overview once a day,
and you ask for details. Actions on cards (confirm, change a category) answer
in the same chat. Local only. Email text shown here is untrusted, so templates
escape everything, and form posts from other websites are rejected.
"""
import json
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import re

from doraemon import budgets as bud
from doraemon.agents import AGENTS, COLORS, Brain, shades, short_clock as clock, styled
from doraemon.assistant import last_full_month, month_name
from doraemon.chat import AgentChat
from doraemon.calendar_sync import Calendar, Schedule, connect_calendar, connect_schedule, event_body
from doraemon.config import Settings
from doraemon.db import Database, from_chat, item_model, transaction_model
from doraemon.display import describe_when, zone_name
from doraemon.fx import FxRates
from doraemon.google_auth import NotConnected
from doraemon.ledger import build_ledger, spending_totals
from doraemon.llm import get_backend
from doraemon.reminders import (DEFAULT_FIRST, DEFAULT_TIMES, MAX_TIMES, Messenger, ReminderJob, created_text,
                                TIMED_STEPS, defaults_for, list_text, plan, short_time, steps_for, wants_reminder)
from doraemon.rules import RuleStore, name_matches
from doraemon.schema import Category, ItemType
from doraemon.telegram import Command, CommandListener, connect_telegram
from doraemon.web import charts

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=HERE / "templates")
TEMPLATES.env.filters["shades"] = shades
TRAVEL = {ItemType.FLIGHT.value, ItemType.OTHER_TRAVEL.value}
# Chip colours per item type: (background, text, label)
CHIPS = {
    "bill": ("#FDE8E7", "#A3241F", "Bill"),
    "appointment": ("#E3F0FB", "#0B5A96", "Appointment"),
    "deadline": ("#FFF1D6", "#8A5300", "Deadline"),
    "delivery": ("#E6F5EA", "#1E6B3A", "Delivery"),
    "rsvp": ("#EFE9FB", "#5B3BA6", "RSVP"),
    "flight": ("#FDE4EF", "#9C2160", "Trip"),
    "hotel": ("#FDE4EF", "#9C2160", "Trip"),
    "other_travel": ("#FDE4EF", "#9C2160", "Trip"),
    "reminder": ("#E0F2FE", "#075985", "Reminder"),
}
CUSTOM_CHIP = ("#EEF1F6", "#4A5D70")
# The Type dropdown, grouped by where each type goes once you confirm it
TYPE_GROUPS = [
    ("Telegram reminder", [("reminder", "Reminder"), ("bill", "Bill"), ("deadline", "Deadline"),
                           ("delivery", "Delivery"), ("rsvp", "RSVP")]),
    ("Google Calendar event", [("appointment", "Appointment"), ("flight", "Flight"), ("hotel", "Hotel"),
                               ("other_travel", "Train, bus or ferry")]),
]
BUDGET_OPTIONS = [(bud.TOTAL, "All spending")] + [(c.value, c.value.capitalize()) for c in Category]
TYPE_LABELS = {value: label for _, options in TYPE_GROUPS for value, label in options}
# What your own types can work like: the group they join in the dropdown
CUSTOM_BASES = {"reminder": "Telegram reminder", "appointment": "Google Calendar event"}
NEW_TYPE = "__new__"


class CheckJob:
    """Checks Gmail in the background, one check at a time: every few minutes, or when you press Run now.

    Reports back in the agents' chats. Automatic checks speak up only when there's something
    new, and say each problem once instead of every 15 minutes.
    """

    def __init__(self, db: Database, settings: Settings, ingest=None) -> None:
        self.db, self.settings = db, settings
        self.ingest = ingest  # run_ingest; tests pass a fake
        self.lock = threading.Lock()
        self.running = False
        self.last_problem = ""
        self.after_payments = None  # e.g. budget alerts; called when a check brings in new payments

    def start(self, auto: bool = False) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self.run, args=(auto,), daemon=True).start()
        return True

    def run(self, auto: bool) -> None:
        if self.ingest is None:
            from doraemon.ingest import run_ingest  # imports Google libraries only when needed
            self.ingest = run_ingest
        try:
            # never opens a browser from here: an expired sign-in raises NotConnected instead
            summary = self.ingest(self.settings, say=lambda _: None, interactive=False)
        except NotConnected:
            self.problem(auto, "I can't read your Gmail until you approve Google access again. "
                               "Run: python -m doraemon.calendar_sync connect")
        except FileNotFoundError:
            self.problem(auto, "Gmail isn't connected yet. Run python -m doraemon.ingest once to sign in.")
        except Exception as e:  # shown in the chat rather than crashing the page
            self.problem(auto, f"Sorry, checking your inbox failed: {e}")
        else:
            self.report(summary, auto)
        finally:
            self.running = False

    def report(self, summary: dict, auto: bool) -> None:
        items, txns, n = summary["items"], len(summary["transactions"]), summary["processed"]
        if summary.get("stopped"):
            self.problem(auto, f"I read {n} new email(s), then stopped: {summary['stopped']} I'll try again later.")
        elif summary["counts"].get("failed"):
            self.problem(auto, f"{summary['counts']['failed']} email(s) couldn't be read just now. I'll try again later.")
        else:
            self.last_problem = ""
        if items:
            self.db.add_message("calendar", "agent", f"📬 {n} new email(s): {items} new thing(s) for you. "
                                                     "Ask \"What needs my OK?\" to see them.")
        elif not auto and not summary.get("stopped"):
            self.db.add_message("calendar", "agent", f"Checked {n} new email(s): nothing that needs you." if n
                                else "Checked your inbox just now. Nothing new since my last look.")
        if txns:
            self.db.add_message("money", "agent", f"{txns} new payment(s) came in with Dorae-2's inbox check.")
            if self.after_payments:
                self.after_payments()

    def problem(self, auto: bool, text: str) -> None:
        if auto and text == self.last_problem:
            return
        self.last_problem = text
        self.db.add_message("calendar", "agent", text)


def poll_every(job: CheckJob, minutes: int) -> threading.Event:
    """Run the check soon after the app starts, then every few minutes. Set the returned event to stop."""
    stop = threading.Event()

    def loop() -> None:
        wait = 20  # first look shortly after starting, so the app has settled
        while not stop.wait(wait):
            job.start(auto=True)
            wait = minutes * 60
    threading.Thread(target=loop, daemon=True, name="gmail-check").start()
    return stop


def asset_version() -> str:
    """Changes whenever app.css or app.js changes, so browsers fetch the new file instead of a cached one."""
    return str(max(int(f.stat().st_mtime) for f in (HERE / "static").iterdir()))


def create_app(settings: Settings | None = None, fx: FxRates | None = None,
               calendar: Calendar | None = None, schedule: Schedule | None = None, ingest=None,
               chat_backend=None, messenger: Messenger | None = None) -> FastAPI:
    settings = settings or Settings()
    # The page only reads cached rates, so it never waits on the network; ingest fetches them.
    fx = fx or (FxRates(settings.db_path, fetch=None) if settings.convert_currencies else None)
    db = Database(settings.db_path)
    rules = RuleStore(settings.db_path)
    job = CheckJob(db, settings, ingest)
    connected_calendar: list[Calendar] = [calendar] if calendar else []
    connected_schedule: list[Schedule] = [schedule] if schedule else []
    connected_bot: dict = {}  # chat id -> Telegram, so the bot is made once but connecting later still works

    def get_messenger() -> Messenger | None:
        """Your Telegram bot, or None until DORAEMON_TELEGRAM_TOKEN is set and `connect` has run."""
        if messenger is not None:
            return messenger
        bot = connect_telegram(settings, db)
        if bot is None:
            return None
        return connected_bot.setdefault(bot.chat_id, bot)

    reminder_job = ReminderJob(db, settings.timezone, get_messenger)
    # Commands you can send the bot. /remindars is a common misspelling, so it works too.
    bot_commands = CommandListener(db, get_messenger, [
        Command("reminders", "List your reminders", lambda: list_text(db, settings.timezone), ("remindars", "list")),
        Command("help", "What I can do", lambda: bot_commands.help_text(), ("start",)),
    ])

    def get_schedule() -> Schedule | None:
        """Your Google calendars (read-only), or None until you've approved reading them (never opens a browser)."""
        if not connected_schedule:
            try:
                connected_schedule.append(connect_schedule(settings))
            except (NotConnected, FileNotFoundError):
                return None
        return connected_schedule[0]

    brain = Brain(db, settings, fx, schedule=get_schedule)
    if chat_backend is None and settings.chat_model != "off":
        try:
            chat_backend = get_backend(settings.chat_model, settings)
        except ValueError:
            chat_backend = None  # unknown backend: the fixed answers still work
    if chat_backend is not None:
        brain.chat = AgentChat(brain, chat_backend)
    job.after_payments = brain.budget_alerts

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = poll_every(job, settings.poll_minutes) if settings.poll_minutes > 0 else None
        stop_reminders = reminder_job.run_every(60)
        stop_commands = bot_commands.run()
        stop_briefing = brain.run_briefing(60)
        yield
        stop_briefing.set()
        stop_reminders.set()
        stop_commands.set()
        if stop:
            stop.set()

    app = FastAPI(title="Doraemon", lifespan=lifespan)
    app.state.db, app.state.rules, app.state.settings, app.state.job = db, rules, settings, job
    app.state.reminders, app.state.bot_commands = reminder_job, bot_commands
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tz = ZoneInfo(settings.timezone)
    home = settings.home_currency

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        # Any website could make your browser post a form to localhost; only accept our own pages.
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin and urlparse(origin).netloc != request.url.netloc:
                return PlainTextResponse("Cross-site request blocked", status_code=403)
        return await call_next(request)

    def wants_json(request: Request) -> bool:
        return request.headers.get("x-requested-with") == "fetch"

    # --- view helpers -------------------------------------------------------

    def local_time(iso: str) -> str:
        dt = datetime.fromisoformat(iso).astimezone(tz)
        if dt.date() == datetime.now(tz).date():
            return dt.strftime("%I:%M %p").lstrip("0")
        return dt.strftime("%a") if (datetime.now(tz).date() - dt.date()).days < 7 else dt.strftime("%d %b").lstrip("0")

    def type_options() -> list[tuple[str, list[tuple[str, str]]]]:
        """TYPE_GROUPS with your own types added to the group they work like."""
        mine: dict[str, list] = {}
        for t in db.custom_types():
            mine.setdefault(CUSTOM_BASES.get(t["base"], ""), []).append((f"custom:{t['id']}", t["name"]))
        return [(group, options + mine.get(group, [])) for group, options in TYPE_GROUPS]

    def card(row) -> dict:
        item = item_model(row)
        local = item.start_at.astimezone(ZoneInfo(item.timezone)) if item.start_at else None
        bg, fg, kind = CHIPS.get(row["type"], ("#EEF1F6", "#4A5D70", row["type"]))
        custom = db.custom_type(row["custom_type_id"]) if row["custom_type_id"] else None
        if custom:
            bg, fg, kind = *CUSTOM_CHIP, custom["name"]
        detail = describe_when(item, settings.timezone)
        if row["amount"]:
            detail += f" · {row['currency']} {row['amount']}"
        if row["location"]:
            detail += f" · {row['location']}"
        sender = db.conn.execute("SELECT sender FROM processed_emails WHERE message_id = ?",
                                 (row["gmail_id"],)).fetchone()
        remind = wants_reminder(row["type"]) and item.start_at is not None
        first, times = defaults_for(row["type"])
        draft = remind and row["type"] == ItemType.REMINDER and from_chat(row["gmail_id"])
        return {
            "row": row, "detail": detail, "chip_bg": bg, "chip_fg": fg, "kind": kind,
            "type_value": f"custom:{custom['id']}" if custom else row["type"],
            "remind": remind, "first": first, "times": times, "max_times": MAX_TIMES,
            # a reminder you asked for in chat: its card is a form you fill in, not a suggestion to confirm
            "draft": draft,
            # a draft offers every step, since you might add a time to it; all-day items use whole days
            "steps": TIMED_STEPS if draft else steps_for(item) if remind else [],
            "reminders": [short_time(datetime.fromisoformat(r["remind_at"]), tz) for r in db.reminders(row["id"])],
            "sender": sender["sender"] if sender else "",
            "is_travel": row["type"] in TRAVEL,
            "date": local.date().isoformat() if local else "",
            "time": "" if item.all_day or not local else local.strftime("%H:%M"),
            "upcoming": item.start_at is None or item.start_at.astimezone(tz).date() >= datetime.now(tz).date(),
        }

    def payment_line(row) -> dict:
        txn = transaction_model(row)
        return {"row": row, "day": row["purchased_at"][:10], "category": row["category"],
                "home_amount": fx.to_home(txn, home) if fx and row["currency"] != home else None}

    def message_view(row) -> dict:
        payload = json.loads(row["payload"]) if row["payload"] else None
        view = {"id": row["id"], "role": row["role"], "text": row["text"], "kind": row["kind"],
                "time": local_time(row["created_at"]), "payload": payload, "agent": row["agent"]}
        if row["kind"] in ("items", "agenda") and payload:
            rows = [db.get("item", i) for i in payload["ids"]]
            view["cards"] = [card(r) for r in rows if r]
            view["more"] = payload.get("more", 0)
        elif row["kind"] == "payments" and payload:
            view["lines"] = [payment_line(r) for r in (db.get("transaction", i) for i in payload["ids"]) if r]
            view["home"] = home
        elif row["kind"] == "breakdown" and payload:
            view["donut"] = charts.donut(payload["rows"], payload["currency"])
        elif row["kind"] == "trend" and payload:
            view["chart"] = charts.columns(payload)
        elif row["kind"] == "budgets" and payload:
            view["budget_options"] = BUDGET_OPTIONS
        elif row["kind"] == "briefing" and payload:
            view["days"] = [{**d, "events": [event_line(e, d["date"]) for e in d["events"]]} for d in payload["days"]]
            view["bills"] = [card(r) for r in (db.get("item", i) for i in payload["bills"]) if r]
            view["trips"] = [card(r) for r in (db.get("item", i) for i in payload["trips"]) if r]
            view["reminders"] = [{**r, "when": clock(r["time"])} for r in payload["reminders"]]
        elif row["kind"] == "week" and payload:
            view["days"] = week_days(payload)
            view["calendars"] = {e["calendar"]: e["color"] for e in payload["events"]}
        return view

    def event_line(e: dict, iso: str) -> dict:
        """One event on one day of the briefing: its time there ('9am–10am' or 'All day') and the rest of it."""
        timed = bool(e["start"]) and iso == e["first"]
        when = (clock(e["start"]) + (f"–{clock(e['end'])}" if e["end"] and e["end"] != e["start"] else "")
                if timed else "All day")
        return {**e, "when": when}

    def week_days(payload: dict) -> list[dict]:
        """Seven day columns; an event spanning days shows on each, with its time only on the first."""
        first, today = date.fromisoformat(payload["first"]), datetime.now(tz).date()
        days = []
        for d in (first + timedelta(days=i) for i in range(7)):
            iso, events = d.isoformat(), []
            for e in payload["events"]:
                if not e["first"] <= iso <= e["last"]:
                    continue
                timed = bool(e["start"]) and iso == e["first"]
                when = (clock(e["start"]) + (f"–{clock(e['end'])}" if e["end"] and e["end"] != e["start"] else "")
                        if timed else "All day")
                tip = " · ".join(x for x in (when, e["title"], e["calendar"], e["location"]) if x)
                events.append({**e, "time": clock(e["start"]) if timed else "", "all_day": not timed, "tip": tip})
            events.sort(key=lambda e: (not e["all_day"], e["start"]))
            days.append({"name": d.strftime("%a"), "num": d.day, "today": d == today, "past": d < today, "events": events})
        return days

    def sidebar() -> list[dict]:
        entries = []
        for agent in (styled(a, db) for a in AGENTS.values()):
            last = db.last_message(agent.id)
            seen = int(db.get_setting(f"seen:{agent.id}") or 0)
            preview = (last["text"].split("\n")[0] if last and last["text"] else
                       ("Sent a card" if last else agent.role))
            if last and last["role"] == "user":
                preview = "You: " + preview
            entries.append({"agent": agent, "preview": preview,
                            "time": local_time(last["created_at"]) if last else "",
                            "unread": bool(last and last["id"] > seen and last["role"] == "agent")})
        return entries

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {
            "msg": request.query_params.get("msg", ""),
            "sidebar": sidebar(),
            "current": None,
            "gmail_connected": Path(settings.google_token).exists(),
            "account": settings.user_emails[0] if settings.user_emails else "",
            "types": type_options(),
            "asset_version": asset_version(),
            **context,
        })

    def render_messages(request: Request, ids: list[int]) -> str:
        rows = [r for r in (db.conn.execute("SELECT * FROM chat_messages WHERE id = ?", (i,)).fetchone() for i in ids) if r]
        tpl = TEMPLATES.get_template("_message_list.html")
        return tpl.render(messages=[message_view(r) for r in rows], types=type_options())

    def render_card(item_id: int, back: str) -> str:
        """One item's card as it is now, to swap in for the old one after an edit."""
        tpl = TEMPLATES.env.from_string('{% from "_macros.html" import item_card %}{{ item_card(c, types, back) }}')
        return tpl.render(c=card(db.get("item", item_id)), types=type_options(), back=back)

    def answer(request: Request, agent: str, back: str, text: str, mood: str = "idle", remove: bool = False,
               refresh: int | None = None, reply_card: dict | None = None):
        """An action's result, said by the agent in its chat. `refresh`: an item whose card changed.
        `reply_card`: a card ({"kind", "payload"}) to show under the text."""
        msg_id = db.add_message(agent, "agent", text, *((reply_card["kind"], reply_card["payload"]) if reply_card else ()))
        if wants_json(request):
            return JSONResponse({"html": render_messages(request, [msg_id]), "mood": mood, "remove": remove,
                                 **({"card": render_card(refresh, back)} if refresh else {})})
        path = back if back.startswith("/") and not back.startswith("//") else f"/chat/{agent}"
        if not path.startswith("/chat/"):  # list pages show it too; chat pages already have it in the thread
            path += ("&" if "?" in path else "?") + "msg=" + quote(text)
        return RedirectResponse(path, status_code=303)

    # --- Google Calendar ----------------------------------------------------

    def get_calendar() -> Calendar | None:
        """The Doraemon calendar, or None until you've approved Calendar access (never opens a browser)."""
        if not connected_calendar:
            try:
                connected_calendar.append(connect_calendar(settings, db, interactive=False))
            except (NotConnected, FileNotFoundError):
                return None
        return connected_calendar[0]

    def add_to_calendar(item_id: int) -> str:
        row = db.get("item", item_id)
        item = item_model(row)
        title = row["title"]
        if item.start_at is None:
            return f"Saved “{title}”. It has no date, so I didn't add it to your calendar."
        cal = get_calendar()
        if cal is None:
            return (f"Saved “{title}”. To put it in Google Calendar, connect it once: "
                    "python -m doraemon.calendar_sync connect")
        try:
            event_id = cal.upsert(row["calendar_event_id"], event_body(item, item_id, row["gmail_id"]))
        except Exception as e:  # the item stays confirmed and can be retried
            text = str(e)
            if "has not been used" in text or "is disabled" in text or "accessNotConfigured" in text:
                text = "the Google Calendar API is turned off in your Google Cloud project"
            return f"Saved “{title}”, but I couldn't add it to Google Calendar ({text[:160]}). Ask me to retry from Upcoming."
        db.set_calendar_event(item_id, event_id)
        return f"Added “{title}” to your Doraemon calendar. 🔔"

    # --- Telegram reminders ---------------------------------------------------

    def set_reminders(item_id: int, first: str, times: int) -> tuple[bool, str]:
        """Plan the item's reminders. Returns (any planned, what Dorae-2 says about it)."""
        row = db.get("item", item_id)
        item, now = item_model(row), datetime.now(tz)
        planned, passed = plan(item, settings.timezone, first, times, now)
        title = row["title"]
        if not planned:
            return False, (f"Those reminder times for “{title}” have already passed. "
                           "Pick a closer one, like 1 hour before or at the time.")
        db.set_reminders(item_id, planned)
        whens = [f"{short_time(r['at'], tz)} ({r['label']})" for r in planned]
        listed = whens[0] if len(whens) == 1 else ", ".join(whens[:-1]) + " and " + whens[-1]
        times_text = "once" if len(planned) == 1 else "twice" if len(planned) == 2 else f"{len(planned)} times"
        text = f"I'll remind you about “{title}” on Telegram {times_text}: {listed}. 🔔"
        if passed:
            text += f" (Skipped {', '.join(passed)}: that's already passed.)"
        bot = get_messenger()
        if bot is None:
            text += (" Telegram isn't connected yet, so connect it before then: set DORAEMON_TELEGRAM_TOKEN "
                     "and run python -m doraemon.telegram connect.")
        else:
            try:  # the reminders are saved either way; this just lets you know on your phone
                bot.send(created_text(item, settings.timezone, planned, now, updated=row["status"] == "confirmed"))
            except Exception as e:
                text += f" (I couldn't send the confirmation on Telegram: {str(e)[:120]}. The reminders are still set.)"
        return True, text

    def first_and_times(first: str, times: str) -> tuple[str, int]:
        try:
            n = int(times)
        except ValueError:
            n = DEFAULT_TIMES
        return first or DEFAULT_FIRST, max(1, min(n, MAX_TIMES))

    # --- agent chats ----------------------------------------------------------

    @app.get("/")
    def home_page():
        return RedirectResponse(f"/chat/{db.get_setting('last_agent') or 'calendar'}", status_code=303)

    @app.get("/agents", response_class=HTMLResponse)
    def agents_list(request: Request):
        return render(request, "agents.html")

    @app.get("/chat/{agent_id}", response_class=HTMLResponse)
    def chat(request: Request, agent_id: str):
        agent = AGENTS.get(agent_id)
        if agent is None:
            raise HTTPException(404)
        brain.ensure_overview(agent_id)
        rows = db.messages(agent_id)
        if rows:
            db.set_setting(f"seen:{agent_id}", str(rows[-1]["id"]))
        db.set_setting("last_agent", agent_id)
        last = last_full_month(settings)
        suggestions = [s.format(last_month=month_name(last)) for s in agent.suggestions]
        return render(request, "chat.html", current=styled(agent, db), messages=with_separators(rows),
                      suggestions=suggestions, panel=panel(agent_id))

    def with_separators(rows) -> list[dict]:
        """Message views with a centred time label wherever 20+ minutes passed, like a phone chat."""
        views, previous = [], None
        for row in rows:
            view = message_view(row)
            at = datetime.fromisoformat(row["created_at"]).astimezone(tz)
            if previous is None or (at - previous).total_seconds() > 20 * 60:
                day = "Today" if at.date() == datetime.now(tz).date() else at.strftime("%a %d %b").replace(" 0", " ")
                view["separator"] = f"{day} {at.strftime('%I:%M %p').lstrip('0')}"
            previous = at
            views.append(view)
        return views

    # --- customize an agent (name and colour) ---------------------------------

    @app.get("/agents/{agent_id}/customize", response_class=HTMLResponse)
    def customize(request: Request, agent_id: str):
        agent = AGENTS.get(agent_id)
        if agent is None:
            raise HTTPException(404)
        return render(request, "customize.html", current=None, editing=styled(agent, db), colors=COLORS,
                      defaults=agent)

    @app.post("/agents/{agent_id}/customize")
    def save_customize(agent_id: str, name: str = Form(...), color: str = Form(...)):
        agent = AGENTS.get(agent_id)
        if agent is None:
            raise HTTPException(404)
        name = name.strip()[:24] or agent.name
        color = color if re.fullmatch(r"#[0-9A-Fa-f]{6}", color) else agent.color
        db.set_setting(f"agent:{agent_id}:name", None if name == agent.name else name)
        db.set_setting(f"agent:{agent_id}:color", None if color.upper() == agent.color.upper() else color.upper())
        return RedirectResponse(f"/chat/{agent_id}", status_code=303)

    def panel(agent_id: str) -> dict:
        if agent_id == "money":
            month = brain.this_month()
            b = brain.breakdown(month)["payload"]
            if not b["count"]:
                b = brain.breakdown(last_full_month(settings))["payload"]
            meters = brain.budget_card()["payload"]["rows"]
            return {"kind": "money", "label": b["label"], "total": Decimal(b["total"]), "count": b["count"],
                    "donut": charts.donut(b["rows"], home), "currency": home, "meters": meters,
                    "has_budgets": bool(meters)}
        upcoming, past = brain.pending()
        week = [card(db.get("item", i)) for i in brain.agenda(7)]
        checked = db.get_setting("gmail_checked_at")
        every = f"Every {settings.poll_minutes} min" if settings.poll_minutes > 0 else "When you press Run now"
        return {"kind": "calendar", "waiting": len(upcoming), "passed": len(past), "week": week,
                "calendar_connected": get_calendar() is not None, "checking": job.running,
                "telegram": get_messenger() is not None,
                "briefing_when": f"Every day, {clock(brain.briefing_time().strftime('%H:%M'))}"
                                 if brain.briefing_time() else "",
                "check_when": every + (f" · last {local_time(checked)}" if checked else "")}

    @app.post("/chat/{agent_id}/ask")
    def ask(request: Request, agent_id: str, q: str = Form(...)):
        if agent_id not in AGENTS:
            raise HTTPException(404)
        ids = brain.reply(agent_id, q.strip()[:500])
        db.set_setting(f"seen:{agent_id}", str(ids[-1]))
        if wants_json(request):
            return JSONResponse({"html": render_messages(request, ids)})
        return RedirectResponse(f"/chat/{agent_id}", status_code=303)

    @app.post("/chat/{agent_id}/clear")
    def clear(agent_id: str):
        if agent_id not in AGENTS:
            raise HTTPException(404)
        db.clear_messages(agent_id)
        db.set_setting(f"overview:{agent_id}", None)  # start fresh with a new overview
        return RedirectResponse(f"/chat/{agent_id}", status_code=303)

    @app.get("/chat/{agent_id}/new")
    def new_messages(request: Request, agent_id: str, after: int = 0):
        """Messages posted since the page loaded, e.g. by the automatic inbox check."""
        if agent_id not in AGENTS:
            raise HTTPException(404)
        rows = db.messages(agent_id, after_id=after)
        if rows:
            db.set_setting(f"seen:{agent_id}", str(rows[-1]["id"]))
        return JSONResponse({"html": render_messages(request, [r["id"] for r in rows]),
                             "last": rows[-1]["id"] if rows else after})

    @app.post("/check")
    def check():
        started = job.start()
        return JSONResponse({"started": started})

    @app.get("/check/status")
    def check_status():
        return JSONResponse({"running": job.running})

    # --- item actions (Dorae-2) -----------------------------------------------

    @app.post("/items/{item_id}/confirm")
    def confirm(request: Request, item_id: int, back: str = Form("/chat/calendar")):
        row = db.get("item", item_id)
        if wants_reminder(row["type"]) and row["start_at"]:  # bills, deadlines, deliveries: Telegram, not Calendar
            first, times = defaults_for(row["type"])
            return remind(request, item_id, first=first, times=str(times), back=back, title="", day="", at="",
                          type="", new_type="", new_base="reminder")
        db.update("item", item_id, "confirm", status="confirmed")
        msg = add_to_calendar(item_id) if not wants_reminder(row["type"]) else             f"Saved “{row['title']}”. It has no date, so I can't set a reminder: add one under Edit."
        if not db.counts().get("proposed", 0):
            msg += " That's everything. All clear!"
        return answer(request, "calendar", back, msg, mood="happy", remove=True)

    @app.post("/items/{item_id}/remind")
    def remind(request: Request, item_id: int, first: str = Form(DEFAULT_FIRST), times: str = Form(str(DEFAULT_TIMES)),
               back: str = Form("/chat/calendar"), title: str = Form(""), day: str = Form(""), at: str = Form(""),
               type: str = Form(""), new_type: str = Form(""), new_base: str = Form("reminder")):
        """Confirm the item with Telegram reminders, or change the reminders of one already confirmed.
        A reminder drafted in chat also sends the title, type, date and time you may have edited on its card."""
        row = db.get("item", item_id)
        if row is None:
            raise HTTPException(404)
        made = ""
        if type:
            picked, made = pick_type(type, new_type, new_base)
            if picked is None:
                return answer(request, "calendar", back, made, refresh=item_id)
            if not wants_reminder(picked["type"]):  # now a calendar event: saved, and the card shows Confirm
                db.update("item", item_id, "edit", **picked, edited=1)
                return answer(request, "calendar", back, f"“{row['title']}” is now a Google Calendar event.{made} "
                              "Press Confirm to add it.", refresh=item_id)
            if {k: row[k] for k in picked} != picked:
                db.update("item", item_id, "edit", **picked, edited=1)
        if day:
            try:
                start = datetime.combine(date.fromisoformat(day), time.fromisoformat(at) if at else time(0),
                                         ZoneInfo(row["timezone"]))
            except ValueError:
                return answer(request, "calendar", back, "That date or time doesn't look right. Please check the card.")
            changes = {"title": title.strip()[:80] or row["title"], "start_at": start.isoformat(), "all_day": 0 if at else 1}
            changed = {k: v for k, v in changes.items() if row[k] != v}
            if changed:
                db.update("item", item_id, "edit", **changed, edited=1)
        ok, msg = set_reminders(item_id, *first_and_times(first, times))
        msg += made
        if not ok:
            return answer(request, "calendar", back, msg)
        newly = row["status"] != "confirmed"
        if newly:
            db.update("item", item_id, "confirm", status="confirmed")
            if not db.counts().get("proposed", 0):
                msg += " That's everything. All clear!"
        return answer(request, "calendar", back, msg, mood="happy", remove=newly)

    @app.post("/items/{item_id}/dismiss")
    def dismiss(request: Request, item_id: int, ignore_sender: str = Form(""), back: str = Form("/chat/calendar")):
        row = db.get("item", item_id)
        db.update("item", item_id, "dismiss", status="dismissed")
        if not ignore_sender:
            return answer(request, "calendar", back, f"Okay, I'll skip “{row['title']}”.", remove=True)
        sender = db.conn.execute("SELECT sender FROM processed_emails WHERE message_id = ?",
                                 (row["gmail_id"],)).fetchone()["sender"]
        rule = rules.add("ignore_sender", sender, created_from=f"item #{item_id}")
        others = [r for r in db.items(("proposed",)) if r["sender"] == sender]
        for other in others:
            db.update("item", other["id"], "dismiss (ignored sender)", status="dismissed")
        extra = f" and {len(others)} more from them" if others else ""
        return answer(request, "calendar", back,
                      f"Skipped “{row['title']}”{extra}. I won't read emails from {rule.match} again.", remove=True)

    def pick_type(value: str, new_type: str = "", new_base: str = "reminder") -> tuple[dict | None, str]:
        """The Type dropdown's choice as item fields ({"type", "custom_type_id"}), plus a note if you made a
        new type. On a problem: (None, what to tell you)."""
        made = ""
        if value == NEW_TYPE:  # your own type: kept by name, and works like the built-in type you picked
            name = " ".join(new_type.split())[:30]
            if not name:
                return None, "Give your new type a name, like Study or Gym."
            builtin = next((v for v, label in TYPE_LABELS.items() if name.lower() in (v, label.lower())), None)
            if builtin:  # "Bill" is already a type
                value = builtin
            else:
                is_new = name.lower() not in {t["name"].lower() for t in db.custom_types()}
                custom = db.add_custom_type(name, new_base if new_base in CUSTOM_BASES else "reminder")
                value = f"custom:{custom['id']}"
                if is_new:
                    made = f" New type “{custom['name']}” saved: it works like a {CUSTOM_BASES[custom['base']]}."
        if value.startswith("custom:"):
            custom = db.custom_type(int(value.split(":", 1)[1]))
            if custom is None:
                return None, "That type was deleted. Please pick another."
            return {"type": custom["base"], "custom_type_id": custom["id"]}, made
        try:
            return {"type": ItemType(value).value, "custom_type_id": None}, made
        except ValueError:
            return None, "I don't know that type. Please pick one from the list."

    @app.post("/items/{item_id}/edit")
    def edit(request: Request, item_id: int, title: str = Form(...), type: str = Form(...),
             day: str = Form(""), at: str = Form(""), zone: str = Form(""), back: str = Form("/chat/calendar"),
             new_type: str = Form(""), new_base: str = Form("reminder")):
        row = db.get("item", item_id)
        zone = zone.strip() or row["timezone"]
        try:
            item_tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return answer(request, "calendar", back, f"I don't know the timezone {zone!r}. Try a name like Asia/Tokyo.")
        picked, made = pick_type(type, new_type, new_base)
        if picked is None:
            return answer(request, "calendar", back, made, refresh=item_id)
        changes = {"title": title.strip() or row["title"], **picked, "timezone": zone, "edited": 1}
        if day:
            start = datetime.combine(date.fromisoformat(day), time.fromisoformat(at) if at else time(0), item_tz)
            changes |= {"start_at": start.isoformat(), "all_day": 0 if at else 1}
        db.update("item", item_id, "edit", **changes)
        msg = f"Updated “{changes['title']}”." + made
        if row["type"] in TRAVEL and row["departs_from"] and zone != row["timezone"]:
            rule = rules.add("travel_timezone", row["departs_from"], zone, created_from=f"item #{item_id}")
            msg += f" I'll remember departures from {rule.match} are in {zone_name(zone)} time."
        return answer(request, "calendar", back, msg, refresh=item_id)

    @app.post("/items/{item_id}/sync")
    def sync_item(request: Request, item_id: int):
        return answer(request, "calendar", "/upcoming", add_to_calendar(item_id))

    # --- payment actions (Dorae-1) --------------------------------------------

    @app.post("/transactions/{txn_id}/category")
    def set_category(request: Request, txn_id: int, category: str = Form(...), back: str = Form("/chat/money")):
        row = db.get("transaction", txn_id)
        category = Category(category).value
        db.update("transaction", txn_id, "change category", category=category, edited=1)
        try:
            rule = rules.add("merchant_category", row["merchant"], category, created_from=f"transaction #{txn_id}")
        except ValueError:
            return answer(request, "money", back, f"Moved {row['merchant']} to {category}.")
        # Apply the new rule to this merchant's other payments right away
        same = [r for r in db.transactions() if r["id"] != txn_id and r["status"] == "counted"
                and not r["edited"] and r["category"] != category and name_matches(rule.match, r["merchant"])]
        for other in same:
            db.update("transaction", other["id"], "change category (rule)", category=category)
        extra = f" and {len(same)} other payment(s) from them" if same else ""
        return answer(request, "money", back,
                      f"Moved {row['merchant']}{extra} to {category}. I'll file this merchant there from now on.")

    @app.post("/transactions/{txn_id}/remove")
    def remove(request: Request, txn_id: int, never: str = Form(""), back: str = Form("/chat/money")):
        row = db.get("transaction", txn_id)
        db.update("transaction", txn_id, "remove", status="removed", edited=1)
        if not never:
            return answer(request, "money", back, f"Removed {row['merchant']} {row['amount']} from your spending.", remove=True)
        rule = rules.add("not_spending", row["merchant"], created_from=f"transaction #{txn_id}")
        same = [r for r in db.transactions() if r["id"] != txn_id and r["status"] == "counted"
                and name_matches(rule.match, r["merchant"])]
        for other in same:
            db.update("transaction", other["id"], "remove (rule)", status="removed")
        return answer(request, "money", back,
                      f"Removed {1 + len(same)} payment(s). '{rule.match}' won't count as spending again.", remove=True)

    @app.post("/transactions/{txn_id}/restore")
    def restore(request: Request, txn_id: int, back: str = Form("/spending")):
        db.update("transaction", txn_id, "restore", status="counted", edited=1)
        return answer(request, "money", back, "Counted that payment again.")

    # --- full lists, activity and rules -------------------------------------

    @app.get("/pocket", response_class=HTMLResponse)
    def pocket(request: Request, show: str = "upcoming"):
        upcoming, past = brain.pending()
        ids = upcoming if show == "upcoming" else past
        return render(request, "pocket.html", cards=[card(db.get("item", i)) for i in ids],
                      show=show, n_upcoming=len(upcoming), n_past=len(past))

    @app.get("/upcoming", response_class=HTMLResponse)
    def upcoming_page(request: Request):
        cards = [c for c in (card(r) for r in db.items(("confirmed",))) if c["upcoming"]]
        return render(request, "upcoming.html", cards=cards, calendar_connected=get_calendar() is not None,
                      telegram_connected=get_messenger() is not None)

    @app.get("/spending", response_class=HTMLResponse)
    def spending(request: Request, month: str = ""):
        rows = {r["id"]: r for r in db.transactions()}
        months = sorted({r["purchased_at"][:7] for r in rows.values()}, reverse=True)
        in_month = [r for r in rows.values() if not month or r["purchased_at"].startswith(month)]
        counted = [transaction_model(r) for r in in_month if r["status"] == "counted"]
        kept, duplicates = build_ledger(counted)
        dropped = {int(d.dropped.message_id): rows[int(d.kept.message_id)] for d in duplicates}
        category_of = {int(t.message_id): t.category.value for t in kept}  # after receipt enrichment
        sums = spending_totals(kept, home, fx)
        converted = {int(t.message_id): sums.home_amounts[id(t)] for t in kept
                     if t.currency != home and id(t) in sums.home_amounts}
        lines = [{"row": r, "category": category_of.get(r["id"], r["category"]),
                  "duplicate_of": dropped.get(r["id"]), "day": r["purchased_at"][:10],
                  "home_amount": converted.get(r["id"])}
                 for r in sorted(in_month, key=lambda r: r["purchased_at"], reverse=True)]
        return render(request, "spending.html", lines=lines, months=months, month=month, home=home,
                      donut=charts.donut([[c, str(a)] for c, a in sorted(sums.by_category.items(), key=lambda kv: -kv[1])], home),
                      trend=charts.columns(brain.trend()["payload"]),
                      total=sums.total, foreign=sums.unconverted, n_converted=len(converted),
                      categories=[c.value for c in Category])

    # --- budgets (Dorae-1) -------------------------------------------------------

    @app.get("/budgets", response_class=HTMLResponse)
    def budgets_page(request: Request):
        current, avg = db.budgets(), brain.averages()
        card = brain.budget_card()
        spent = spending_totals(brain.ledger(brain.this_month()), home, fx)
        fields = [{"category": c, "label": label, "amount": current.get(c),
                   "average": avg.get(c), "spent": spent.total if c == bud.TOTAL else spent.by_category.get(c)}
                  for c, label in BUDGET_OPTIONS]
        return render(request, "budgets.html", meters=card["payload"]["rows"], month=card["payload"]["label"],
                      fields=fields, home=home, trend=charts.columns(brain.trend()["payload"]),
                      colors=charts.CATEGORY_COLORS, neutral=charts.NEUTRAL)

    @app.post("/budgets")
    async def save_budgets(request: Request):
        """The Budgets page sends every field (blank removes that budget); a chat card sends one draft."""
        form = await request.form()
        back = str(form.get("back") or "/budgets")
        wanted: dict[str, str] = {}
        if form.get("draft_category"):
            wanted[str(form["draft_category"])] = str(form.get("draft_amount", ""))
        else:
            wanted = {c: str(form[f"amount:{c}"]) for c, _ in BUDGET_OPTIONS if f"amount:{c}" in form}
        changes = []
        for category, raw in wanted.items():
            if category not in dict(BUDGET_OPTIONS):
                continue
            try:
                amount = bud.cents(Decimal(raw.replace(",", "").replace("$", "").strip())) if raw.strip() else None
            except ArithmeticError:
                return answer(request, "money", back, f"“{raw}” doesn't look like an amount. Try a number like 300.")
            if amount is not None and amount < 0:
                return answer(request, "money", back, "A budget can't be negative.")
            before = db.budgets().get(category)
            if (amount or None) != before:
                db.set_budget(category, amount)
                name = "overall" if category == bud.TOTAL else category
                changes.append(f"{name} {home} {amount:,.2f}" if amount else f"no {name} budget")
        if not changes:
            return answer(request, "money", back, "No changes to your budgets.")
        card = brain.budget_card()
        text = "Saved: " + ", ".join(changes) + ". " + (card["text"] if card["payload"]["rows"] else "")
        brain.budget_alerts(quiet=True)  # the card shows where you stand; alert only on later payments
        return answer(request, "money", back, text.strip(), mood="happy", reply_card=card)

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        entries = []
        for a in db.recent_actions(50):
            row = db.get(a["kind"], a["row_id"])
            label = (row["title"] if a["kind"] == "item" else f"{row['merchant']} {row['currency']} {row['amount']}") if row else "?"
            entries.append({"a": a, "label": label})
        return render(request, "history.html", entries=entries)

    @app.post("/undo/{log_id}")
    def undo(request: Request, log_id: int):
        entry = db.undo(log_id)
        msg = f"Undid '{entry['action']}'. Rules it created are kept; delete them in Learned rules."
        if entry["kind"] == "item" and entry["action"] == "confirm":
            row = db.get("item", entry["row_id"])
            if row and db.cancel_reminders(row["id"]):
                msg = f"Undid confirming “{row['title']}” and cancelled its reminders."
            if row and row["calendar_event_id"]:
                cal = get_calendar()
                try:
                    if cal:
                        cal.delete(row["calendar_event_id"])
                    db.set_calendar_event(row["id"], None)
                    msg = f"Undid confirming “{row['title']}” and removed it from your calendar."
                except Exception as e:
                    msg = f"Undid confirming “{row['title']}”, but couldn't remove the calendar event ({str(e)[:120]})."
        agent = "calendar" if entry["kind"] == "item" else "money"
        db.add_message(agent, "agent", msg)
        return RedirectResponse("/history?msg=" + quote(msg), status_code=303)

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request):
        return render(request, "rules.html", mine=rules.user_rules(), n_defaults=len(rules.defaults),
                      custom_types=db.custom_types(), bases=CUSTOM_BASES)

    @app.post("/types/{type_id}/delete")
    def delete_type(type_id: int):
        row = db.custom_type(type_id)
        db.delete_custom_type(type_id)
        text = f"Deleted the type “{row['name']}”. Its items keep working as before." if row else "Type deleted."
        return RedirectResponse("/rules?msg=" + quote(text), status_code=303)

    @app.post("/rules/{rule_id}/delete")
    def delete_rule(rule_id: int):
        rules.delete(rule_id)
        return RedirectResponse("/rules?msg=" + quote("Rule deleted."), status_code=303)

    return app
