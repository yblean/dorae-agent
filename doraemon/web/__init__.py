"""Doraemon's web app: chat with Dorae-1 (spending) and Dorae-2 (calendar).

    python -m doraemon.web        # then open http://127.0.0.1:8000

Everything starts in an agent's chat: each posts a short overview once a day,
and you ask for details. Actions on cards (confirm, change a category) answer
in the same chat. Local only. Email text shown here is untrusted, so templates
escape everything, and form posts from other websites are rejected.
"""
import json
import threading
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import re

from doraemon.agents import AGENTS, COLORS, Brain, shades, styled
from doraemon.assistant import last_full_month, month_name
from doraemon.calendar_sync import Calendar, connect_calendar, event_body
from doraemon.config import Settings
from doraemon.db import Database, item_model, transaction_model
from doraemon.display import describe_when, zone_name
from doraemon.fx import FxRates
from doraemon.google_auth import NotConnected
from doraemon.ledger import build_ledger, spending_totals
from doraemon.rules import RuleStore, name_matches
from doraemon.schema import Category, ItemType

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
}


class CheckJob:
    """'Check now': one background ingest at a time; reports back in the agents' chats."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.lock = threading.Lock()
        self.running = False

    def start(self, settings: Settings) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, args=(settings,), daemon=True).start()
        return True

    def _run(self, settings: Settings) -> None:
        from doraemon.ingest import run_ingest  # imports Google libraries only when needed
        try:
            summary = run_ingest(settings, days=3, say=lambda _: None)
            items, txns = summary["items"], len(summary["transactions"])
            if not summary["processed"]:
                text = "Checked your inbox just now. Nothing new since my last look."
            else:
                text = f"Checked {summary['processed']} new email(s): " + (
                    f"{items} new thing(s) for you. Ask \"What needs my OK?\" to see them." if items
                    else "nothing that needs you.")
            self.db.add_message("calendar", "agent", text)
            if txns:
                self.db.add_message("money", "agent", f"{txns} new payment(s) came in with Dorae-2's inbox check.")
        except FileNotFoundError:
            self.db.add_message("calendar", "agent", "Gmail isn't connected yet. Run python -m doraemon.ingest once to sign in.")
        except Exception as e:  # shown in the chat rather than crashing the page
            self.db.add_message("calendar", "agent", f"Sorry, checking your inbox failed: {e}")
        finally:
            self.running = False


def asset_version() -> str:
    """Changes whenever app.css or app.js changes, so browsers fetch the new file instead of a cached one."""
    return str(max(int(f.stat().st_mtime) for f in (HERE / "static").iterdir()))


def create_app(settings: Settings | None = None, fx: FxRates | None = None,
               calendar: Calendar | None = None) -> FastAPI:
    settings = settings or Settings()
    # The page only reads cached rates, so it never waits on the network; ingest fetches them.
    fx = fx or (FxRates(settings.db_path, fetch=None) if settings.convert_currencies else None)
    db = Database(settings.db_path)
    rules = RuleStore(settings.db_path)
    brain = Brain(db, settings, fx)
    job = CheckJob(db)
    connected_calendar: list[Calendar] = [calendar] if calendar else []
    app = FastAPI(title="Doraemon")
    app.state.db, app.state.rules, app.state.settings = db, rules, settings
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

    def card(row) -> dict:
        item = item_model(row)
        local = item.start_at.astimezone(ZoneInfo(item.timezone)) if item.start_at else None
        bg, fg, kind = CHIPS.get(row["type"], ("#EEF1F6", "#4A5D70", row["type"]))
        detail = describe_when(item, settings.timezone)
        if row["amount"]:
            detail += f" · {row['currency']} {row['amount']}"
        if row["location"]:
            detail += f" · {row['location']}"
        sender = db.conn.execute("SELECT sender FROM processed_emails WHERE message_id = ?",
                                 (row["gmail_id"],)).fetchone()
        return {
            "row": row, "detail": detail, "chip_bg": bg, "chip_fg": fg, "kind": kind,
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

    def bars(rows: list[list[str]]) -> list[tuple[str, Decimal, int]]:
        amounts = [(c, Decimal(a)) for c, a in rows]
        biggest = max((a for _, a in amounts), default=Decimal(1)) or Decimal(1)
        return [(c, a, max(2, int(a / biggest * 100)) if a > 0 else 0) for c, a in amounts]

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
            view["bars"] = bars(payload["rows"])
        return view

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
            "types": [t.value for t in ItemType],
            "asset_version": asset_version(),
            **context,
        })

    def render_messages(request: Request, ids: list[int]) -> str:
        rows = [r for r in (db.conn.execute("SELECT * FROM chat_messages WHERE id = ?", (i,)).fetchone() for i in ids) if r]
        tpl = TEMPLATES.get_template("_message_list.html")
        return tpl.render(messages=[message_view(r) for r in rows], types=[t.value for t in ItemType])

    def answer(request: Request, agent: str, back: str, text: str, mood: str = "idle", remove: bool = False):
        """An action's result, said by the agent in its chat."""
        msg_id = db.add_message(agent, "agent", text)
        if wants_json(request):
            return JSONResponse({"html": render_messages(request, [msg_id]), "mood": mood, "remove": remove})
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
            return {"kind": "money", "label": b["label"], "total": Decimal(b["total"]), "count": b["count"],
                    "bars": bars(b["rows"]), "currency": home}
        upcoming, past = brain.pending()
        week = [card(db.get("item", i)) for i in brain.agenda(7)]
        return {"kind": "calendar", "waiting": len(upcoming), "passed": len(past), "week": week,
                "calendar_connected": get_calendar() is not None, "checking": job.running}

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

    @app.post("/check")
    def check():
        started = job.start(settings)
        return JSONResponse({"started": started})

    @app.get("/check/status")
    def check_status():
        return JSONResponse({"running": job.running})

    # --- item actions (Dorae-2) -----------------------------------------------

    @app.post("/items/{item_id}/confirm")
    def confirm(request: Request, item_id: int, back: str = Form("/chat/calendar")):
        db.update("item", item_id, "confirm", status="confirmed")
        msg = add_to_calendar(item_id)
        if not db.counts().get("proposed", 0):
            msg += " That's everything. All clear!"
        return answer(request, "calendar", back, msg, mood="happy", remove=True)

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

    @app.post("/items/{item_id}/edit")
    def edit(request: Request, item_id: int, title: str = Form(...), type: str = Form(...),
             day: str = Form(""), at: str = Form(""), zone: str = Form(""), back: str = Form("/chat/calendar")):
        row = db.get("item", item_id)
        zone = zone.strip() or row["timezone"]
        try:
            item_tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return answer(request, "calendar", back, f"I don't know the timezone {zone!r}. Try a name like Asia/Tokyo.")
        changes = {"title": title.strip() or row["title"], "type": ItemType(type).value,
                   "timezone": zone, "edited": 1}
        if day:
            start = datetime.combine(date.fromisoformat(day), time.fromisoformat(at) if at else time(0), item_tz)
            changes |= {"start_at": start.isoformat(), "all_day": 0 if at else 1}
        db.update("item", item_id, "edit", **changes)
        msg = f"Updated “{changes['title']}”."
        if row["type"] in TRAVEL and row["departs_from"] and zone != row["timezone"]:
            rule = rules.add("travel_timezone", row["departs_from"], zone, created_from=f"item #{item_id}")
            msg += f" I'll remember departures from {rule.match} are in {zone_name(zone)} time."
        return answer(request, "calendar", back, msg)

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
        return render(request, "upcoming.html", cards=cards, calendar_connected=get_calendar() is not None)

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
                      bars=bars([[c, str(a)] for c, a in sorted(sums.by_category.items(), key=lambda kv: -kv[1])]),
                      total=sums.total, foreign=sums.unconverted, n_converted=len(converted),
                      categories=[c.value for c in Category])

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
        return render(request, "rules.html", mine=rules.user_rules(), n_defaults=len(rules.defaults))

    @app.post("/rules/{rule_id}/delete")
    def delete_rule(rule_id: int):
        rules.delete(rule_id)
        return RedirectResponse("/rules?msg=" + quote("Rule deleted."), status_code=303)

    return app
