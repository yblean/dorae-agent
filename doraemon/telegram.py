"""Your Telegram bot, which sends you Doraemon's reminders.

One-time setup: create a bot with @BotFather, put its token in .env as
DORAEMON_TELEGRAM_TOKEN, then run

    python -m doraemon.telegram connect   # then send your bot any message, e.g. /start
    python -m doraemon.telegram test      # sends a test message

`connect` remembers your chat with the bot, so reminders only ever go to you. While the web app
is open the bot also answers commands from that chat (e.g. /reminders); other chats get no answer.
Messages are sent as plain text; the token is never printed or put in error messages.
"""
import argparse
import logging
import sys
import threading
import time
from typing import Callable

import httpx

from doraemon.config import Settings
from doraemon.db import Database

API = "https://api.telegram.org"
CHAT_KEY = "telegram_chat_id"
OFFSET_KEY = "telegram_update_offset"  # updates before this were already answered
STALE_SECONDS = 600  # commands sent while the app was closed are ignored after 10 minutes

log = logging.getLogger(__name__)


class TelegramError(Exception):
    """A failed call, described without the request URL (it contains the bot token)."""


class Telegram:
    def __init__(self, token: str, chat_id: str | None = None, client: httpx.Client | None = None) -> None:
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.Client(timeout=40)

    def call(self, method: str, wait: float | None = None, **params) -> dict | list:
        """`wait`: seconds to wait for Telegram (default 40). Not getUpdates' `timeout` param, which is long polling."""
        try:
            resp = self.client.post(f"{API}/bot{self.token}/{method}", json=params,
                                    **({"timeout": wait} if wait else {}))
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"couldn't reach Telegram ({type(e).__name__})") from None
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"Telegram said no (HTTP {resp.status_code})")
        return data["result"]

    def send(self, text: str) -> None:
        if not self.chat_id:
            raise TelegramError("no chat yet: run python -m doraemon.telegram connect")
        self.call("sendMessage", wait=10, chat_id=self.chat_id, text=text[:4000],
                  link_preview_options={"is_disabled": True})

    def updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        """New messages to the bot, waiting up to `timeout` seconds for one (long polling)."""
        return self.call("getUpdates", wait=timeout + 15, timeout=timeout, allowed_updates=["message"],
                         **({"offset": offset} if offset else {}))

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """The menu Telegram shows when you type "/"."""
        self.call("setMyCommands", wait=10, commands=[{"command": c, "description": d} for c, d in commands])

    def wait_for_chat(self, seconds: int = 180) -> dict | None:
        """The first private chat that messages the bot within `seconds`, or None."""
        offset, end = None, time.monotonic() + seconds
        while time.monotonic() < end:
            updates = self.call("getUpdates", timeout=min(30, max(1, int(end - time.monotonic()))),
                                **({"offset": offset} if offset else {}), allowed_updates=["message"])
            for u in updates:
                offset = u["update_id"] + 1
                chat = (u.get("message") or {}).get("chat") or {}
                if chat.get("type") == "private":
                    self.call("getUpdates", offset=offset, timeout=0)  # mark it read
                    return chat
        return None


class Command:
    def __init__(self, name: str, description: str, answer: Callable[[], str], aliases: tuple[str, ...] = ()) -> None:
        self.name, self.description, self.answer, self.aliases = name, description, answer, aliases


class CommandListener:
    """Answers commands you send the bot while the web app is open. Only your linked chat gets answers."""

    def __init__(self, db: Database, bot: Callable[[], Telegram | None], commands: list[Command]) -> None:
        self.db, self.bot = db, bot  # bot: () -> your Telegram, or None until connected
        self.commands = commands
        self.menu_set_for: str | None = None

    def lookup(self, text: str) -> Command | None:
        word = text.strip().split()[0].lower().split("@")[0].lstrip("/") if text.strip() else ""
        return next((c for c in self.commands if word == c.name or word in c.aliases), None)

    def help_text(self) -> str:
        return "I'm Doraemon's reminder bot. Commands:\n" + "\n".join(f"/{c.name}: {c.description}" for c in self.commands)

    def poll_once(self, timeout: int = 25) -> int:
        """Wait for new messages and answer them. Returns how many were answered."""
        bot = self.bot()
        if bot is None or not hasattr(bot, "updates"):
            return 0
        if self.menu_set_for != bot.chat_id:
            bot.set_commands([(c.name, c.description) for c in self.commands])
            self.menu_set_for = bot.chat_id
        offset = int(self.db.get_setting(OFFSET_KEY) or 0) or None
        answered = 0
        for u in bot.updates(offset, timeout):
            self.db.set_setting(OFFSET_KEY, str(u["update_id"] + 1))
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(bot.chat_id):
                continue  # someone else found the bot: they get nothing
            if time.time() - msg.get("date", 0) > STALE_SECONDS:
                continue
            command = self.lookup(msg.get("text", ""))
            bot.send(command.answer() if command else self.help_text())
            answered += 1
        return answered

    def run(self) -> threading.Event:
        stop = threading.Event()

        def loop() -> None:
            while not stop.is_set():
                if self.bot() is None:
                    stop.wait(60)  # not connected yet: look again in a minute
                    continue
                try:
                    self.poll_once()
                except Exception as e:  # network down, or another copy of the app is polling ("Conflict")
                    log.warning("Telegram commands: %s", e)
                    stop.wait(30)
        threading.Thread(target=loop, daemon=True, name="telegram-commands").start()
        return stop


def connect_telegram(settings: Settings, db: Database) -> Telegram | None:
    """Your bot, or None until a token is set and `connect` has found your chat."""
    chat_id = settings.telegram_chat_id or db.get_setting(CHAT_KEY)
    if not settings.telegram_token or not chat_id:
        return None
    return Telegram(settings.telegram_token, chat_id)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m doraemon.telegram")
    ap.add_argument("command", choices=["connect", "test"])
    args = ap.parse_args()
    settings = Settings()
    if not settings.telegram_token:
        sys.exit("Set DORAEMON_TELEGRAM_TOKEN in .env first (create a bot with @BotFather to get one).")
    db = Database(settings.db_path)
    if args.command == "test":
        bot = connect_telegram(settings, db)
        if bot is None:
            sys.exit("Not connected yet. Run: python -m doraemon.telegram connect")
        bot.send("👋 Test from Doraemon. Reminders will arrive here.")
        print("Sent. Check Telegram.")
        return

    bot = Telegram(settings.telegram_token)
    me = bot.call("getMe")
    print(f"Open https://t.me/{me['username']} in Telegram and send it any message (e.g. /start).")
    print("Waiting up to 3 minutes...")
    chat = bot.wait_for_chat()
    if chat is None:
        sys.exit("No message arrived. Run this again and message the bot while it waits.")
    who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")
    db.set_setting(CHAT_KEY, str(chat["id"]))
    bot.chat_id = str(chat["id"])
    bot.send("✅ Connected to Doraemon. Your reminders will arrive here.")
    print(f"Connected to {who}'s chat. Reminders will be sent there while the web app is running.")


if __name__ == "__main__":
    main()
