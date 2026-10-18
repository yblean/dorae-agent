"""Your Telegram bot, which sends you Doraemon's reminders.

One-time setup: create a bot with @BotFather, put its token in .env as
DORAEMON_TELEGRAM_TOKEN, then run

    python -m doraemon.telegram connect   # then send your bot any message, e.g. /start
    python -m doraemon.telegram test      # sends a test message
    python -m doraemon.telegram photo     # gives the bot Dorae's picture (connect does it if it has none)

`connect` remembers your chat with the bot, so reminders only ever go to you. While Doraemon runs
the bot also answers that chat: commands (e.g. /reminders), the buttons under reminders (Done,
Snooze), and plain messages ("remind me to call mom at 5pm", "how much did I spend this week?",
see doraemon.phone). Other chats get no answer. Messages are sent as plain text; the token is never
printed or put in error messages.
"""
import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from doraemon.config import Settings
from doraemon.db import Database

API = "https://api.telegram.org"
PHOTO = Path(__file__).parent / "web" / "static" / "dorae-telegram.jpg"  # Dorae in Telegram's blues: the bot's photo
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

    def upload(self, method: str, fields: dict, files: dict) -> dict | list:
        """Like call(), with files sent as multipart/form-data (Telegram's way to upload)."""
        try:
            resp = self.client.post(f"{API}/bot{self.token}/{method}", data=fields, files=files, timeout=30)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"couldn't reach Telegram ({type(e).__name__})") from None
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"Telegram said no (HTTP {resp.status_code})")
        return data["result"]

    def has_photo(self) -> bool:
        me = self.call("getMe")
        return self.call("getUserProfilePhotos", user_id=me["id"], limit=1)["total_count"] > 0

    def set_photo(self, path: Path = PHOTO) -> None:
        """Give the bot a profile photo (a JPG; Telegram crops it to a circle)."""
        self.upload("setMyProfilePhoto", {"photo": json.dumps({"type": "static", "photo": "attach://photo"})},
                    {"photo": (path.name, path.read_bytes(), "image/jpeg")})

    def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        """`buttons`: rows of (label, data); pressing one sends `data` back to the bot."""
        if not self.chat_id:
            raise TelegramError("no chat yet: run python -m doraemon.telegram connect")
        markup = {"reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row]
                                                       for row in buttons]}} if buttons else {}
        self.call("sendMessage", wait=10, chat_id=self.chat_id, text=text[:4000],
                  link_preview_options={"is_disabled": True}, **markup)

    def updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        """New messages and button presses, waiting up to `timeout` seconds for one (long polling)."""
        return self.call("getUpdates", wait=timeout + 15, timeout=timeout,
                         allowed_updates=["message", "callback_query"], **({"offset": offset} if offset else {}))

    def answer_button(self, press_id: str, text: str) -> None:
        """Stops the button's loading spinner, showing `text` briefly."""
        self.call("answerCallbackQuery", wait=10, callback_query_id=press_id, text=text[:200])

    def edit(self, message_id: int, text: str) -> None:
        """Replace a message's text, which also removes its buttons."""
        self.call("editMessageText", wait=10, chat_id=self.chat_id, message_id=message_id, text=text[:4000],
                  link_preview_options={"is_disabled": True})

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


Buttons = list[list[tuple[str, str]]]


class CommandListener:
    """Answers commands you send the bot, other messages and the buttons you press, while Doraemon runs.
    Only your linked chat gets answers."""

    def __init__(self, db: Database, bot: Callable[[], Telegram | None], commands: list[Command],
                 on_button: Callable[[str], str] | None = None,
                 on_text: Callable[[str], tuple[str, Buttons | None]] | None = None,
                 examples: tuple[str, ...] = ()) -> None:
        self.db, self.bot = db, bot  # bot: () -> your Telegram, or None until connected
        self.commands = commands
        self.on_button = on_button  # a button's data -> what to say, e.g. "done:12" -> "✅ Done: Pay bill"
        self.on_text = on_text  # a message that isn't a command -> (reply, buttons); None: the command list
        self.examples = examples  # messages to suggest in the help text
        self.menu_set_for: str | None = None

    def lookup(self, text: str) -> Command | None:
        word = text.strip().split()[0].lower().split("@")[0].lstrip("/") if text.strip() else ""
        return next((c for c in self.commands if word == c.name or word in c.aliases), None)

    def help_text(self) -> str:
        text = "I'm Doraemon's bot. "
        if self.examples:
            text += "Send me things like:\n" + "\n".join(f"• {e}" for e in self.examples) + "\n\n"
        return text + "Commands:\n" + "\n".join(f"/{c.name}: {c.description}" for c in self.commands)

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
            if "callback_query" in u:
                answered += self.press(bot, u["callback_query"])
                continue
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(bot.chat_id):
                continue  # someone else found the bot: they get nothing
            if time.time() - msg.get("date", 0) > STALE_SECONDS:
                continue
            text = msg.get("text", "")
            command = self.lookup(text) if text.strip().startswith("/") or self.on_text is None else None
            if command:
                bot.send(command.answer())
            elif self.on_text and text.strip():
                bot.send(*self.on_text(text))
            else:
                bot.send(self.help_text())
            answered += 1
        return answered

    def press(self, bot: Telegram, press: dict) -> int:
        """A button under one of our messages. Pressed long after it was sent still counts: the item decides."""
        msg = press.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(bot.chat_id) or self.on_button is None:
            return 0
        reply = self.on_button(press.get("data", ""))
        bot.answer_button(press["id"], reply)
        if msg.get("message_id"):
            bot.edit(msg["message_id"], f"{msg.get('text', '')}\n\n{reply}".strip())
        return 1

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
    ap.add_argument("command", choices=["connect", "test", "photo"])
    args = ap.parse_args()
    settings = Settings()
    if not settings.telegram_token:
        sys.exit("Set DORAEMON_TELEGRAM_TOKEN in .env first (create a bot with @BotFather to get one).")
    db = Database(settings.db_path)
    if args.command == "photo":
        try:
            Telegram(settings.telegram_token).set_photo()
        except TelegramError as e:
            sys.exit(f"Couldn't set the bot's picture: {e}")
        print("Done: your bot now has Dorae's picture (Telegram may take a minute to show it).")
        return
    if args.command == "test":
        bot = connect_telegram(settings, db)
        if bot is None:
            sys.exit("Not connected yet. Run: python -m doraemon.telegram connect")
        bot.send("👋 Test from Doraemon. Reminders will arrive here.\nPress the button to check Doraemon answers it.",
                 buttons=[[("👍 Press me", "test:1")]])
        print("Sent. Check Telegram and press the button: the message should change to say buttons work\n"
              "(only while Doraemon runs: python -m doraemon.service, or the web app).")
        return

    bot = Telegram(settings.telegram_token)
    me = bot.call("getMe")
    try:  # a new bot gets Dorae's picture; one you've given a picture keeps it
        if not bot.has_photo():
            bot.set_photo()
            print("Gave your bot Dorae's picture.")
    except TelegramError as e:  # not needed to connect
        print(f"(Couldn't set the bot's picture: {e}. Try later: python -m doraemon.telegram photo)")
    print(f"Open https://t.me/{me['username']} in Telegram and send it any message (e.g. /start).")
    print("Waiting up to 3 minutes...")
    chat = bot.wait_for_chat()
    if chat is None:
        sys.exit("No message arrived. Run this again and message the bot while it waits.")
    who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")
    db.set_setting(CHAT_KEY, str(chat["id"]))
    bot.chat_id = str(chat["id"])
    bot.send("✅ Connected to Doraemon. Your reminders will arrive here.")
    print(f"Connected to {who}'s chat. Reminders will be sent there while Doraemon runs (python -m doraemon.service, or the web app).")


if __name__ == "__main__":
    main()
