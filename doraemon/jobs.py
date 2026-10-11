"""Everything Doraemon does on its own: check Gmail, send Telegram reminders, answer the bot,
post the morning briefing and budget alerts.

Both the background service (python -m doraemon.service) and the web app can run these jobs,
but only one at a time: whichever holds data/jobs.lock. The service comes first: a web app
holding the jobs hands them over within a minute of the service starting. When the service
stops, the web app (if open) takes them back. The lock belongs to the process, so if it crashes
or is killed, the operating system lets go of the lock.
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from doraemon.agents import Brain
from doraemon.calendar_sync import Schedule, connect_schedule
from doraemon.config import Settings
from doraemon.db import Database
from doraemon.fx import FxRates
from doraemon.google_auth import NotConnected
from doraemon.reminders import Messenger, ReminderJob, list_text, mark_done, snooze
from doraemon.telegram import Command, CommandListener, connect_telegram

log = logging.getLogger(__name__)

OWNER_KEY = "jobs:owner"          # which program runs the jobs, and when it last said so
SERVICE_KEY = "jobs:service"      # when the background service last said it's there (it gets the jobs first)
HEARTBEAT = 30                    # seconds between looking for the lock / saying "still here"
FRESH = timedelta(minutes=3)      # an owner not heard from for this long has stopped


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

    def __init__(self, settings: Settings, fx: FxRates | None = None, schedule: Schedule | None = None,
                 ingest=None, messenger: Messenger | None = None) -> None:
        self.settings = settings
        # Pages only read cached rates, so they never wait on the network; ingest fetches them.
        self.fx = fx or (FxRates(settings.db_path, fetch=None) if settings.convert_currencies else None)
        self.db = Database(settings.db_path)
        self.messenger = messenger
        self.bots: dict = {}  # chat id -> Telegram, so the bot is made once but connecting later still works
        self.schedules: list[Schedule] = [schedule] if schedule else []
        self.brain = Brain(self.db, settings, self.fx, schedule=self.get_schedule)
        self.check = CheckJob(self.db, settings, ingest)
        self.check.after_payments = self.brain.budget_alerts
        self.reminders = ReminderJob(self.db, settings.timezone, self.get_messenger, settings.remind_until_done)
        # Commands you can send the bot. /remindars is a common misspelling, so it works too.
        self.commands = CommandListener(self.db, self.get_messenger, [
            Command("reminders", "List your reminders", lambda: list_text(self.db, settings.timezone),
                    ("remindars", "list")),
            Command("help", "What I can do", lambda: self.commands.help_text(), ("start",)),
        ], on_button=self.button)
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

    def get_schedule(self) -> Schedule | None:
        """Your Google calendars (read-only), or None until you've approved reading them (never opens a browser)."""
        if not self.schedules:
            try:
                self.schedules.append(connect_schedule(self.settings))
            except (NotConnected, FileNotFoundError):
                return None
        return self.schedules[0]

    # --- what the jobs do -------------------------------------------------------------

    def button(self, data: str) -> str:
        """A Done or Snooze button pressed under a Telegram reminder. Returns what to show there."""
        action, _, ident = data.partition(":")
        log.info("Telegram button pressed: %s", action)
        if action == "test":  # from python -m doraemon.telegram test
            return f"👍 Buttons work. The {self.name or 'app'} answered."
        if not ident.isdigit():
            return "I don't know that button."
        item_id = int(ident)
        if action == "done":
            text = mark_done(self.db, item_id)
            if text.startswith("✅"):
                self.db.add_message("calendar", "agent", f"{text}. (You pressed Done on Telegram.)")
            return text
        if action == "snooze":
            return snooze(self.db, item_id, self.settings.timezone)
        return "I don't know that button."

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

    # --- running the jobs, in one program at a time -------------------------------------

    def start_jobs(self) -> None:
        s = self.settings
        if s.poll_minutes > 0:  # first look shortly after starting, so the program has settled
            self.stops.append(every(s.poll_minutes * 60, lambda: self.check.start(auto=True), "gmail-check", first=20))
        self.stops.append(every(60, self.reminders.tick, "reminders", first=5))
        self.stops.append(self.commands.run())
        self.stops.append(every(60, self.briefing_tick, "briefing", first=10))

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
