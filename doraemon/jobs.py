"""Everything Doraemon does on its own: check Gmail, send Telegram reminders, answer the bot (doraemon.phone),
post the morning briefing and budget alerts, sync brokerage accounts, ask for the monthly
net worth check-in, tidy "needs your OK" (doraemon.tidy) and warn you on Telegram when
something stops working (the model, a sign-in).

Both the background service (python -m doraemon.service) and the web app can run these jobs,
but only one at a time: whichever holds data/jobs.lock. The service comes first: a web app
holding the jobs hands them over within a minute of the service starting. When the service
stops, the web app (if open) takes them back. The lock belongs to the process, so if it crashes
or is killed, the operating system lets go of the lock.
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from doraemon.agents import Brain, money
from doraemon.assistant import month_name
from doraemon.brokers import Broker, BrokerError, connected, sync
from doraemon.calendar_sync import Calendar, Schedule, connect_calendar, connect_schedule
from doraemon.config import Settings
from doraemon.db import Database
from doraemon.fx import FxRates
from doraemon.google_auth import NotConnected
from doraemon.llm import model_ready, ollama_up
from doraemon.phone import EXAMPLES, Phone
from doraemon.reminders import (SNOOZE_MINUTES, SNOOZE_TOMORROW, Messenger, ReminderJob, list_text, mark_done,
                                short_time, snooze)
from doraemon.telegram import Command, CommandListener, connect_telegram
from doraemon import senders
from doraemon.db import from_chat
from doraemon.rules import RuleStore
from doraemon.fitness import TARGET_NAMES, import_steps
from doraemon import coach, nudges
from doraemon.phone import ASK_KEY
from doraemon.tidy import tidy

log = logging.getLogger(__name__)

OWNER_KEY = "jobs:owner"          # which program runs the jobs, and when it last said so
SERVICE_KEY = "jobs:service"      # when the background service last said it's there (it gets the jobs first)
HEARTBEAT = 30                    # seconds between looking for the lock / saying "still here"
FRESH = timedelta(minutes=3)      # an owner not heard from for this long has stopped
BROKER_HOURS = 3                  # how often brokerage accounts are synced
SESSION_WARNING = timedelta(days=3)  # warn this long before a sign-in (Webull's session) runs out


class FileLock:
    """A lock only one process can hold. The operating system releases it when the process ends."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.file = None

    def acquire(self) -> bool:
        if self.file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.file = f
        return True

    def release(self) -> None:
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.file.close()
        self.file = None


class CheckJob:
    """Checks Gmail in the background, one check at a time: every few minutes, or when you press Run now.

    Reports back in the agents' chats. Automatic checks speak up only when there's something
    new, and say each problem once instead of every 15 minutes. A check already running in the
    other program (service or web app) counts too, so the same emails are never read twice at once.
    """

    def __init__(self, db: Database, settings: Settings, ingest=None) -> None:
        self.db, self.settings = db, settings
        self.ingest = ingest  # run_ingest; tests pass a fake
        self.lock = threading.Lock()
        self.across = FileLock(Path(settings.db_path).parent / "check.lock")
        self.running = False
        self.last_problem = ""
        self.after_payments = None  # e.g. budget alerts; called when a check brings in new payments
        self.on_problem = None  # e.g. a Telegram message; called once per new problem of an automatic check
        self.after_items = None  # e.g. sending them to Telegram; called when a check finds new things for you
        # Is the model running (starting Ollama if not)? Tests pass a fake ingest, which needs no model.
        self.ready = (lambda: True) if ingest else (lambda: model_ready(settings))

    def start(self, auto: bool = False) -> bool:
        with self.lock:
            if self.running or not self.across.acquire():
                return False
            self.running = True
        threading.Thread(target=self.run, args=(auto,), daemon=True).start()
        return True

    def run(self, auto: bool) -> None:
        if self.ingest is None:
            from doraemon.ingest import run_ingest  # imports Google libraries only when needed
            self.ingest = run_ingest
        try:
            if not self.ready():
                self.problem(auto, f"I couldn't check your inbox: the model ({self.settings.model.partition(':')[2]}) "
                                   "isn't running, and I couldn't start Ollama. Open the Ollama app; I'll try again "
                                   "at the next check.")
                return
            # never opens a browser from here: an expired sign-in raises NotConnected instead
            summary = self.ingest(self.settings, say=lambda _: None, interactive=False)
        except NotConnected:
            self.problem(auto, "I can't read your Gmail until you approve Google access again. "
                               "Run: python -m doraemon.calendar_sync connect")
        except FileNotFoundError:
            self.problem(auto, "Gmail isn't connected yet. Run python -m doraemon.ingest once to sign in.")
        except Exception as e:  # shown in the chat rather than crashing
            self.problem(auto, f"Sorry, checking your inbox failed: {e}")
        else:
            self.report(summary, auto)
        finally:
            self.running = False
            self.across.release()

    def report(self, summary: dict, auto: bool) -> None:
        items, txns, n = summary["items"], len(summary["transactions"]), summary["processed"]
        if summary.get("stopped"):
            self.problem(auto, f"I read {n} new email(s), then stopped: {summary['stopped']} I'll try again later.")
        elif summary["counts"].get("failed"):
            self.problem(auto, f"{summary['counts']['failed']} email(s) couldn't be read just now. I'll try again later.")
        else:
            self.last_problem = ""
        held = summary.get("held", 0)
        quietly = f" ({held} more held back as newsletters or mass mail, under All items → Held back)" if held else ""
        if items:
            self.db.add_message("calendar", "agent", f"📬 {n} new email(s): {items} new thing(s) for you{quietly}. "
                                                     "Ask \"What needs my OK?\" to see them.")
            if self.after_items:
                self.after_items()
        elif not auto and not summary.get("stopped"):
            self.db.add_message("calendar", "agent", f"Checked {n} new email(s): nothing that needs you{quietly}." if n
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
        if auto and self.on_problem:
            self.on_problem(text)


def every(seconds: float, work, name: str, first: float | None = None) -> threading.Event:
    """Run `work` every `seconds` (first after `first` seconds) on a thread until the returned event is set."""
    stop = threading.Event()

    def loop() -> None:
        wait = seconds if first is None else first
        while not stop.wait(wait):
            try:
                work()
            except Exception:  # keep the loop alive; the next round tries again
                log.exception("%s failed", name)
            wait = seconds
    threading.Thread(target=loop, daemon=True, name=name).start()
    return stop


class Runtime:
    """The database, Dorae-1 and Dorae-2's brain, and the jobs, set up once for a web app or the service."""

    def __init__(self, settings: Settings, fx: FxRates | None = None, calendar: Calendar | None = None,
                 schedule: Schedule | None = None,
                 ingest=None, messenger: Messenger | None = None, brokers: list[Broker] | None = None) -> None:
        self.settings = settings
        # Pages only read cached rates, so they never wait on the network; ingest fetches them.
        self.fx = fx or (FxRates(settings.db_path, fetch=None) if settings.convert_currencies else None)
        self.db = Database(settings.db_path)
        self.messenger = messenger
        self.bots: dict = {}  # chat id -> Telegram, so the bot is made once but connecting later still works
        self.calendars: list[Calendar] = [calendar] if calendar else []
        self.schedules: list[Schedule] = [schedule] if schedule else []
        self.brain = Brain(self.db, settings, self.fx, schedule=self.get_schedule)
        self.brain.calendar = self.get_calendar
        self.check = CheckJob(self.db, settings, ingest)
        self.check.after_payments = self.after_payments
        self.check.on_problem = self.warn
        self.check.after_items = self.announce_new
        self.rules = RuleStore(settings.db_path)  # for "Skip emails from ..." (doraemon.senders)
        self.reminders = ReminderJob(self.db, settings.timezone, self.get_messenger, settings.remind_until_done)
        self.phone = Phone(self.brain, self.get_calendar, help_text=lambda: self.commands.help_text())  # plain messages to the bot
        # Commands you can send the bot. /remindars is a common misspelling, so it works too.
        self.commands = CommandListener(self.db, self.get_messenger, [
            Command("reminders", "List your reminders", lambda: list_text(self.db, settings.timezone),
                    ("remindars", "list")),
            Command("status", "Is everything working?", lambda: self.status_text(), ("health",)),
            Command("help", "What I can do", lambda: self.commands.help_text(), ("start",)),
        ], on_button=self.button, on_text=self.phone.answer, on_photo=self.phone.photo, examples=EXAMPLES)
        self.brokers = brokers  # None: the ones set up in .env (doraemon.brokers.connected)
        self.broker_problem = ""
        self.lock = FileLock(Path(settings.db_path).parent / "jobs.lock")
        self.stops: list[threading.Event] = []
        self.name = ""

    # --- connections ----------------------------------------------------------------

    def get_messenger(self) -> Messenger | None:
        """Your Telegram bot, or None until DORAEMON_TELEGRAM_TOKEN is set and `connect` has run."""
        if self.messenger is not None:
            return self.messenger
        bot = connect_telegram(self.settings, self.db)
        if bot is None:
            return None
        return self.bots.setdefault(bot.chat_id, bot)

    def get_calendar(self) -> Calendar | None:
        """The Doraemon calendar, or None until you've approved Calendar access (never opens a browser)."""
        if not self.calendars:
            try:
                self.calendars.append(connect_calendar(self.settings, self.db, interactive=False))
            except (NotConnected, FileNotFoundError):
                return None
        return self.calendars[0]

    def get_schedule(self) -> Schedule | None:
        """Your Google calendars (read-only), or None until you've approved reading them (never opens a browser)."""
        if not self.schedules:
            try:
                self.schedules.append(connect_schedule(self.settings))
            except (NotConnected, FileNotFoundError):
                return None
        return self.schedules[0]

    def warn(self, text: str) -> None:
        """Tell you on Telegram that something needs you (it's in the agents' chat already)."""
        bot = self.get_messenger()
        if bot is None:
            return
        try:
            bot.send(f"⚠️ {text}")
        except Exception as e:  # the chat has it either way
            log.warning("warning on Telegram: %s", e)

    # --- what the jobs do -------------------------------------------------------------

    def button(self, data: str) -> str | tuple[str, list | None]:
        """A button pressed on Telegram: Done or Snooze under a reminder ("snooze:12:180", or "snooze:12" from
        before there were lengths), or Create/Cancel under a drafted one. Returns what to show there."""
        action, _, rest = data.partition(":")
        ident, _, length = rest.partition(":")
        log.info("Telegram button pressed: %s", action)
        if action == "test":  # from python -m doraemon.telegram test
            return f"👍 Buttons work. The {self.name or 'app'} answered."
        if action == "flog":  # Log under a meal Dorae-3 worked out; Edit stays, for numbers that were off
            text = self.phone.log_food(ident)
            return (text, [[("✏️ Edit", f"fedit:{ident}")]]) if text.startswith("✅") else text
        if action == "fedit":  # Edit under food, logged or not: your next message changes its numbers
            return self.phone.start_edit(ident)
        if action == "fdrop":
            self.clear_meal_question()
            return "✖ Not logged." if self.brain.fit.cancel(ident) else "That one's already logged (or was cancelled)."
        if action == "fmeal":  # Something else under "Chicken rice again?": your next message is what you had
            return self.phone.something_else(ident)
        if action == "fcopy" and length:  # a meal under "Which one again?" after "same as yesterday"
            return self.phone.copy_pressed(ident, length)
        if action == "fskip":  # Skip under "Lunch at X? What did you have?"
            self.clear_meal_question()
            return "👍 Okay, not logging that one."
        if action == "fgoal" and ident in TARGET_NAMES and length.isdigit():  # a change offered with the weekly review
            goals = self.brain.fit.set_targets(**{ident: int(length)})
            text = f"✅ Done: {coach.target_text({ident: goals[ident]})}."
            self.db.add_message("fitness", "agent", f"{text} (You chose that on Telegram.)")
            return text
        if action == "fkeep" and ident in TARGET_NAMES:
            return f"👍 Keeping your {TARGET_NAMES[ident]} target as it is."
        if action == "makeall" and re.fullmatch(r"\d+(?:,\d+)*", ident):  # Add all under a booking screenshot
            return "\n".join(self.phone.create(int(i)) for i in ident.split(","))
        if not ident.isdigit():
            return "I don't know that button."
        item_id = int(ident)
        if action == "done":
            text = mark_done(self.db, item_id)
            if text.startswith("✅"):
                self.db.add_message("calendar", "agent", f"{text}. (You pressed Done on Telegram.)")
            return text
        if action == "snooze" and (length.isdigit() or length in ("", SNOOZE_TOMORROW)):
            return snooze(self.db, item_id, self.settings.timezone,
                          length=int(length) if length.isdigit() else length or SNOOZE_MINUTES)
        if action == "make":
            return self.phone.create(item_id)
        if action == "drop":
            text = self.phone.cancel(item_id)
            self.offer_skip(item_id)
            return text
        if action == "skip":  # "Skip emails from ..." under an offer
            row = self.db.get("item", item_id)
            sender = self.sender_of(row) if row else ""
            if not sender:
                return "I can't tell who sent that one."
            text, _ = senders.skip(self.db, self.rules, sender, created_from="you chose Skip on Telegram")
            self.db.add_message("calendar", "agent", f"{text} (You chose that on Telegram.)")
            return text
        return "I don't know that button."

    def sender_of(self, row) -> str:
        found = self.db.conn.execute("SELECT sender FROM processed_emails WHERE message_id = ?", (row["gmail_id"],)).fetchone()
        return found["sender"] if found and found["sender"] else ""

    def offer_skip(self, item_id: int) -> bool:
        """After a Dismiss on Telegram: the third from a sender you never confirm anything from? Offer to skip them."""
        row = self.db.get("item", item_id)
        sender = self.sender_of(row) if row else ""
        offers = senders.worth_skipping(self.db, self.rules, self.settings.user_emails, sender=sender) if sender else []
        bot = self.get_messenger()
        if not offers or bot is None:
            return False
        senders.mark_offered(self.db, offers)
        try:
            bot.send(senders.offer_text(offers), buttons=[[(f"🚫 Skip emails from {offers[0]['name'][:30]}",
                                                           f"skip:{item_id}")]])
        except Exception as e:
            log.warning("skip offer on Telegram: %s", e)
        return True

    def briefing_tick(self, now: datetime | None = None) -> list[dict]:
        """Post the morning briefing when it's due, and send it on Telegram too."""
        posted = self.brain.morning_tick(now)
        if posted and self.settings.briefing_telegram:
            bot = self.get_messenger()
            if bot is not None:
                try:
                    bot.send(self.brain.briefing_telegram(posted[0]))
                except Exception as e:  # the briefing is in the chat either way
                    self.db.add_message("calendar", "agent", f"I couldn't send this morning's briefing on Telegram "
                                                             f"({str(e)[:120]}).")
        return posted

    def sync_brokers(self) -> list[str]:
        """Update brokerage accounts in your net worth. Problems are said once in Dorae-1's chat."""
        brokers = self.brokers if self.brokers is not None else connected(self.settings)
        if not brokers:
            return []
        online = FxRates(self.settings.db_path) if self.settings.convert_currencies else None  # caches today's rates
        lines = []
        for broker in brokers:
            try:
                lines += sync(self.brain.nw, broker, fx=online)
            except BrokerError as e:
                text = f"I couldn't update your {broker.label} account: {e}."
                if text != self.broker_problem:
                    self.broker_problem = text
                    self.db.add_message("money", "agent", text)
                    self.warn(text)
                continue
            self.broker_problem = ""
        self.db.set_setting("brokers:synced_at", datetime.now(timezone.utc).isoformat())
        return lines

    def check_in_tick(self, now: datetime | None = None) -> bool:
        """On the 1st (from the briefing time), ask once for this month's balances: in Dorae-1's chat and on
        Telegram. Returns whether it asked."""
        now = (now or datetime.now(self.brain.tz)).astimezone(self.brain.tz)
        at = self.brain.briefing_time() or dtime(9)
        month = now.strftime("%Y-%m")
        if now.day != 1 or now.time() < at or self.db.get_setting(f"checkin:{month}"):
            return False
        last = month_name((now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
        card = self.brain.networth_card()
        text = (f"📊 It's the 1st: time for your monthly check-in. Update your bank, CPF and loan balances on the Net "
                f"worth page; anything you enter this week counts for the end of {last}.")
        if card["payload"]:
            text += " " + card["text"]
        self.db.add_message("money", "agent", text, card["kind"] if card["payload"] else "text", card["payload"])
        self.db.set_setting(f"checkin:{month}", now.isoformat())
        bot = self.get_messenger()
        if bot is not None:
            try:
                total = card["payload"]["total"] if card["payload"] else None
                bot.send(f"📊 Monthly check-in: update your balances in Doraemon (Net worth page) so {last}'s net worth "
                         "is right." + (f" Net worth now: {money(Decimal(total), self.brain.home)}." if total else ""))
            except Exception as e:  # it's in Dorae-1's chat either way
                log.warning("check-in on Telegram: %s", e)
        return True

    def tidy_tick(self, now: datetime | None = None) -> str:
        """Clear repeated items and suggestions that passed unanswered, and say so in Dorae-2's chat."""
        text = tidy(self.db, self.settings.timezone, now or datetime.now(timezone.utc), self.settings.expire_passed)
        if text:
            self.db.add_message("calendar", "agent", text)
        self.offer_skips()
        return text

    def offer_skips(self) -> list[dict]:
        """Once per sender: those whose suggestions you've only ever dismissed, offered in Dorae-2's chat and on
        Telegram with a Skip button each. Returns the ones offered."""
        offers = senders.worth_skipping(self.db, self.rules, self.settings.user_emails)[:6]
        if not offers:
            return []
        senders.mark_offered(self.db, offers)
        self.db.add_message("calendar", "agent", senders.offer_text(offers), "skip", {"senders": [
            {"address": o["address"], "name": o["name"], "dismissed": o["dismissed"]} for o in offers]})
        bot = self.get_messenger()
        if bot is not None:
            try:
                bot.send(senders.offer_text(offers), buttons=[[(f"🚫 Skip {o['name'][:32]}", f"skip:{o['item']}")]
                                                             for o in offers])
            except Exception as e:
                log.warning("skip offers on Telegram: %s", e)
        return offers

    ANNOUNCED = "telegram:announced"  # the newest item already sent to Telegram

    def announce_new(self, limit: int = 5) -> int:
        """Send suggestions found since the last time to Telegram, with Confirm / Remind me and Dismiss. The first
        time it only notes where it's up to, so what was waiting before isn't sent all at once. Returns how many."""
        newest = self.db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM action_items").fetchone()[0]
        last = self.db.get_setting(self.ANNOUNCED)
        bot = self.get_messenger() if self.settings.telegram_new_items else None
        if last is None or bot is None:
            self.db.set_setting(self.ANNOUNCED, str(newest))
            return 0
        rows = [r for r in self.db.items(("proposed",)) if r["id"] > int(last) and not from_chat(r["gmail_id"])]
        sent = 0
        for row in sorted(rows, key=lambda r: r["id"])[:limit]:
            try:
                bot.send(*self.phone.new_item(row))
                sent += 1
            except Exception as e:  # they're in the app either way
                log.warning("new item on Telegram: %s", e)
                break
        if len(rows) > limit:
            try:
                bot.send(f"…and {len(rows) - limit} more from your email. Open Doraemon to see them all.")
            except Exception:
                pass
        self.db.set_setting(self.ANNOUNCED, str(newest))
        return sent

    def monthly_tick(self, now: datetime | None = None) -> bool:
        """On the 1st (from the briefing time), last month in review once: in Dorae-1's chat and on Telegram."""
        now = (now or datetime.now(self.brain.tz)).astimezone(self.brain.tz)
        at = self.brain.briefing_time() or dtime(9)
        month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        key = f"summary:{month}"
        if now.day != 1 or now.time() < at or self.db.get_setting(key):
            return False
        card = self.brain.monthly_summary(month)
        self.db.set_setting(key, now.isoformat())
        if card["payload"] is None:  # nothing to sum up
            return False
        self.db.add_message("money", "agent", card["text"], card["kind"], card["payload"])
        bot = self.get_messenger()
        if bot is not None:
            try:
                bot.send(self.phone.plain(card))
            except Exception as e:  # it's in Dorae-1's chat either way
                log.warning("monthly summary on Telegram: %s", e)
        return True

    def steps_tick(self) -> int:
        """Read new step counts from where your iPhone's Shortcut saves Apple Health steps (set on the Fitness page)."""
        return import_steps(self.brain.fit, self.brain.fit.health_path(self.settings.health_folder))

    def fitness_tick(self, now: datetime | None = None) -> bool:
        """Dorae-3's daily check (DORAEMON_FITNESS_CHECK_TIME, 6pm): what's left today and how to close the gap, in its
        chat and on Telegram. Once a day; quiet when you're on track or have no targets. Returns whether it spoke."""
        check = self.settings.fitness_check_time
        now = (now or datetime.now(self.brain.tz)).astimezone(self.brain.tz)
        if check == "off":
            return False
        try:
            at = dtime.fromisoformat(check)
        except ValueError:
            return False
        key = f"fitness:checked:{now.date().isoformat()}"
        if now.time() < at or self.db.get_setting(key):
            return False
        self.db.set_setting(key, now.isoformat())
        self.steps_tick()  # the latest count first
        text = self.brain.fit.evening_check(now.date())
        if not text:
            return False
        self.db.add_message("fitness", "agent", text)
        bot = self.get_messenger()
        if bot is not None:
            try:
                bot.send(text)
            except Exception as e:  # it's in Dorae-3's chat either way
                log.warning("fitness check on Telegram: %s", e)
        return True

    # --- Dorae-3 speaking first (doraemon.nudges) ---------------------------------------------------------------

    PROMPTED, PROMPTED_AT = "fitness:prompted_payment", "fitness:prompted_at"

    def after_payments(self) -> None:
        """A check brought in new payments: budget alerts (Dorae-1), then a meal question (Dorae-3)."""
        self.brain.budget_alerts()
        self.meal_prompt_tick()

    def say(self, nudge: nudges.Nudge, now: datetime, chat: bool = False) -> bool:
        """Send a nudge on Telegram (and to Dorae-3's chat when `chat`). A question it asks is what your next plain
        message answers. Returns whether Telegram got it."""
        if chat:
            self.db.add_message("fitness", "agent", nudge.text)
        bot = self.get_messenger()
        if bot is None:
            return False
        try:
            bot.send(nudge.text, buttons=nudge.buttons)
        except Exception as e:
            log.warning("Dorae-3 on Telegram: %s", e)
            return False
        if nudge.ask:
            self.db.set_setting(ASK_KEY, json.dumps({**nudge.ask, "at": now.isoformat()}))
        return True

    def clear_meal_question(self) -> None:
        try:
            if json.loads(self.db.get_setting(ASK_KEY) or "{}").get("kind") == "meal":
                self.db.set_setting(ASK_KEY, None)
        except ValueError:
            pass

    def meal_prompt_tick(self, now: datetime | None = None) -> bool:
        """After new payments: "Lunch at Kopitiam? Chicken rice again?" on Telegram for one at a food place, at most one
        every 2 hours. The first time it only notes where it's up to. Returns whether it asked."""
        now = (now or datetime.now(self.brain.tz)).astimezone(self.brain.tz)
        newest = self.db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
        last = self.db.get_setting(self.PROMPTED)
        self.db.set_setting(self.PROMPTED, str(newest))
        if last is None or not self.settings.fitness_nudges or self.get_messenger() is None:
            return False
        recent = self.db.get_setting(self.PROMPTED_AT)
        if recent and now - datetime.fromisoformat(recent) < nudges.PROMPT_GAP:
            return False
        for txn in self.db.conn.execute("SELECT * FROM transactions WHERE id > ? ORDER BY purchased_at DESC",
                                        (int(last),)).fetchall():
            if (nudge := nudges.meal_prompt(self.brain.fit, txn, now)) and self.say(nudge, now):
                self.db.set_setting(self.PROMPTED_AT, now.isoformat())
                return True
        return False

    def once(self, now: datetime, name: str, at: tuple[int, int], within: timedelta = timedelta(hours=2)) -> bool:
        """True the first time it's asked today from `at` on (and no later than `within` after it)."""
        start = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        key = f"fitness:nudge:{name}:{now.date().isoformat()}"
        if not start <= now <= start + within or self.db.get_setting(key):
            return False
        self.db.set_setting(key, now.isoformat())
        return True

    def nudge_tick(self, now: datetime | None = None) -> list[str]:
        """Dorae-3's day: your usual breakfast at 9:30, a heads-up before a lunch (at 10am) or dinner (3pm) out on your
        calendar, and the weekly review. Returns what it said, by name."""
        now = (now or datetime.now(self.brain.tz)).astimezone(self.brain.tz)
        fit, said = self.brain.fit, []
        if self.settings.fitness_nudges:
            if self.once(now, "breakfast", nudges.BREAKFAST_AT) and self.get_messenger() is not None \
                    and (nudge := nudges.breakfast_prompt(fit, now)):
                said += ["breakfast"] if self.say(nudge, now) else []
            for lunch, at in ((True, nudges.LUNCH_NOTE_AT), (False, nudges.DINNER_NOTE_AT)):
                if not self.once(now, "lunch" if lunch else "dinner", at):
                    continue
                events, _ = self.brain.calendar_events(now.date(), now.date())
                for event in nudges.meals_out(events, now.date(), lunch):
                    if event["start"] > now.strftime("%H:%M"):
                        self.say(nudges.meal_out_note(fit, event), now, chat=True)
                        said.append(f"meal out: {event['title']}")
        if self.weekly_due(now):
            said += ["weekly"] if self.weekly_review(now) else []
        return said

    def weekly_due(self, now: datetime) -> bool:
        """DORAEMON_FITNESS_REVIEW ("mon 09:00"): once a week, from that day and time."""
        day, _, at = self.settings.fitness_review.strip().lower().partition(" ")
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        try:
            weekday, start = days.index(day[:3]), dtime.fromisoformat(at or "09:00")
        except ValueError:  # "off", or not a day and time
            return False
        key = f"fitness:weekly:{(now.date() - timedelta(days=now.weekday())).isoformat()}"
        if now.weekday() != weekday or now.time() < start or self.db.get_setting(key):
            return False
        self.db.set_setting(key, now.isoformat())
        return True

    def weekly_review(self, now: datetime) -> bool:
        """Last week in Dorae-3's chat and on Telegram, then any target changes worth making: Save on the card in the
        app, or a button on Telegram. Quiet when last week has nothing logged. Returns whether it spoke."""
        fit = self.brain.fit
        card, found = nudges.weekly(fit)
        if card["kind"] != "fitness_review":
            return False
        self.db.add_message("fitness", "agent", "📅 Your week: " + card["text"], card["kind"], card["payload"])
        bot = self.get_messenger()
        if bot is not None:
            try:
                bot.send("📅 Your week: " + self.phone.plain(card))
            except Exception as e:
                log.warning("weekly review on Telegram: %s", e)
        if found:
            goals = fit.targets()
            text = "\n\n".join(p["text"] for p in found)
            self.db.add_message("fitness", "agent", text + "\n\nChange anything and press Save, or leave it as it is.",
                                "targets", {"targets": goals, "suggested": {**goals, **{p["key"]: p["to"] for p in found}}})
            self.say(nudges.Nudge(text, nudges.proposal_buttons(found, goals)), now)
        return True

    def webull_expires(self) -> datetime | None:
        if not (self.settings.webull_app_key and self.settings.webull_app_secret):
            return None
        from doraemon.brokers.webull import session_expires
        return session_expires()

    def session_tick(self, now: datetime | None = None) -> bool:
        """A few days before Webull's approved session runs out, say once how to renew it. Returns whether it did."""
        expires = self.webull_expires()
        now = now or datetime.now(timezone.utc)
        if expires is None or expires - now > SESSION_WARNING:
            return False
        key = f"warned:webull:{expires.date().isoformat()}"
        if self.db.get_setting(key):
            return False
        local = expires.astimezone(self.brain.tz)
        text = (f"Your Webull sign-in {'ran out' if expires <= now else 'runs out'} on {local:%a} {local.day} {local:%b}. "
                "To keep your investments up to date, run: python -m doraemon.brokers.webull, then approve it in the "
                "Webull app.")
        self.db.add_message("money", "agent", text)
        self.warn(text)
        self.db.set_setting(key, now.isoformat())
        return True

    def status_text(self, now: datetime | None = None) -> str:
        """/status on Telegram: is each part working, in one message."""
        now = now or datetime.now(timezone.utc)
        tz = self.brain.tz

        def ago(iso: str | None) -> str:
            if not iso:
                return "never"
            minutes = int((now - datetime.fromisoformat(iso)).total_seconds() // 60)
            return "just now" if minutes < 1 else f"{minutes} min ago" if minutes < 90 else \
                f"{minutes // 60} h ago" if minutes < 48 * 60 else f"{minutes // 1440} days ago"

        lines = ["🩺 Doraemon status", f"Running in the {self.name or 'app'}"]
        problem = self.check.last_problem
        lines.append(f"{'⚠️' if problem else '✅'} Gmail: checked {ago(self.db.get_setting('gmail_checked_at'))}"
                     + (f". Last problem: {problem}" if problem else ""))
        model = self.settings.model.partition(":")[2] or self.settings.model
        up = not self.settings.model.startswith("ollama:") or ollama_up(self.settings.ollama_url, wait=1)
        lines.append(f"{'✅' if up else '⚠️'} Model ({model}): {'running' if up else 'not running'}")
        lines.append("✅ Google Calendar: connected" if self.get_calendar() is not None else
                     "⚠️ Google Calendar: not connected (python -m doraemon.calendar_sync connect)")
        expires = self.webull_expires()
        if self.settings.webull_app_key:
            local = expires.astimezone(tz) if expires else None
            soon = expires is not None and expires - now <= SESSION_WARNING
            lines.append(f"{'⚠️' if soon or expires is None else '✅'} Webull: synced "
                         f"{ago(self.db.get_setting('brokers:synced_at'))}"
                         + (f", sign-in until {local:%a} {local.day} {local:%b}" if local else ", sign-in unknown")
                         + (f". Last problem: {self.broker_problem}" if self.broker_problem else ""))
        upcoming = self.db.upcoming_reminders()
        nxt = f", next {short_time(datetime.fromisoformat(upcoming[0]['remind_at']), tz)}" if upcoming else ""
        lines.append(f"🔔 Reminders: {len({r['item_id'] for r in upcoming})} item(s) to remind you about{nxt}")
        lines.append(f"📬 Waiting for your OK: {len(self.brain.pending()[0])}")
        return "\n".join(lines)

    # --- running the jobs, in one program at a time -------------------------------------

    def start_jobs(self) -> None:
        s = self.settings
        if self.db.get_setting(self.ANNOUNCED) is None:  # new finds from now on go to Telegram, not what's waiting
            self.db.set_setting(self.ANNOUNCED, str(self.db.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM action_items").fetchone()[0]))
        if self.db.get_setting(self.PROMPTED) is None:  # meal questions for payments from now on, not old ones
            self.db.set_setting(self.PROMPTED, str(self.db.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]))
        if s.poll_minutes > 0:  # first look shortly after starting, so the program has settled
            self.stops.append(every(s.poll_minutes * 60, lambda: self.check.start(auto=True), "gmail-check", first=20))
        self.stops.append(every(60, self.reminders.tick, "reminders", first=5))
        self.stops.append(self.commands.run())
        self.stops.append(every(60, self.briefing_tick, "briefing", first=10))
        self.stops.append(every(30 * 60, self.check_in_tick, "check-in", first=15))
        self.stops.append(every(BROKER_HOURS * 3600, self.sync_brokers, "brokers", first=30))
        self.stops.append(every(30 * 60, self.tidy_tick, "tidy", first=25))
        self.stops.append(every(6 * 3600, self.session_tick, "sessions", first=40))
        self.stops.append(every(30 * 60, self.monthly_tick, "monthly-summary", first=20))
        self.stops.append(every(10 * 60, self.steps_tick, "steps", first=30))
        self.stops.append(every(5 * 60, self.fitness_tick, "fitness-check", first=45))
        self.stops.append(every(5 * 60, self.nudge_tick, "fitness-nudges", first=50))

    def stop_jobs(self) -> None:
        for stop in self.stops:
            stop.set()
        self.stops = []

    def heartbeat(self) -> None:
        self.db.set_setting(OWNER_KEY, json.dumps({"by": self.name, "pid": os.getpid(),
                                                   "seen": datetime.now(timezone.utc).isoformat()}))

    def owner(self) -> dict | None:
        """{"by", "pid", "seen"} of the program running the jobs, or None if none has been heard from lately."""
        try:
            owner = json.loads(self.db.get_setting(OWNER_KEY) or "null")
            seen = datetime.fromisoformat(owner["seen"])
        except (ValueError, TypeError, KeyError):
            return None
        return owner if datetime.now(timezone.utc) - seen < FRESH else None

    def service_running(self) -> bool:
        """Has the background service said it's there in the last couple of heartbeats?"""
        try:
            seen = datetime.fromisoformat(self.db.get_setting(SERVICE_KEY) or "")
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - seen).total_seconds() < HEARTBEAT * 2.5

    def supervise(self, name: str) -> threading.Event:
        """Run the jobs as `name` ("background service" or "web app") whenever no other program does.
        The web app steps aside while the background service runs, so the service gets them even if the
        web app started first. Set the returned event to stop them and let go of the lock."""
        stop = threading.Event()
        is_service = name == "background service"

        def loop() -> None:
            while True:
                if is_service:
                    self.db.set_setting(SERVICE_KEY, datetime.now(timezone.utc).isoformat())
                elif self.service_running():
                    if self.stops:  # hand the jobs over: the service takes the lock on its next round
                        log.info("the background service is running: handing Doraemon's jobs to it")
                        self.stop_jobs()
                        self.lock.release()
                    if stop.wait(HEARTBEAT):
                        break
                    continue
                if not self.stops and self.lock.acquire():
                    self.name = name
                    log.info("running Doraemon's jobs in the %s (pid %s)", name, os.getpid())
                    self.start_jobs()
                if self.stops:
                    self.heartbeat()
                if stop.wait(HEARTBEAT):
                    break
            self.stop_jobs()
            if is_service:
                self.db.set_setting(SERVICE_KEY, None)
            if self.lock.file is not None:
                self.db.set_setting(OWNER_KEY, None)
                self.lock.release()
        threading.Thread(target=loop, daemon=True, name="jobs-supervisor").start()
        return stop
