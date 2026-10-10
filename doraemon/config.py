"""Single-user settings, read from the environment or a .env file.

v0 has one user (us), so these replace the `users` table for now.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    timezone: str = os.getenv("DORAEMON_TIMEZONE", "UTC")
    home_currency: str = os.getenv("DORAEMON_HOME_CURRENCY", "USD")
    # Convert foreign spending to home_currency with ECB rates (Frankfurter); "off" to keep it separate
    convert_currencies: bool = os.getenv("DORAEMON_CONVERT_CURRENCIES", "on").lower() != "off"
    # Your addresses: a forwarded receipt counts as your spending only if it was sent to one of these.
    # Gmail ingest adds the connected account automatically.
    user_emails: tuple[str, ...] = tuple(
        e.strip().lower() for e in os.getenv("DORAEMON_USER_EMAILS", "").split(",") if e.strip()
    )
    date_order: str = os.getenv("DORAEMON_DATE_ORDER", "DMY")  # how to read 02/10/26
    model: str = os.getenv("DORAEMON_MODEL", "ollama:qwen3.5:4b")
    # The model behind the agents' chats (it calls read-only tools); "off" for fixed keyword answers
    chat_model: str = os.getenv("DORAEMON_CHAT_MODEL", os.getenv("DORAEMON_MODEL", "ollama:qwen3.5:4b"))
    db_path: str = os.getenv("DORAEMON_DB", "data/doraemon.db")  # your rules; never committed
    # OAuth client from Google Cloud, and the sign-in token saved after you approve access
    google_credentials: str = os.getenv("DORAEMON_GOOGLE_CREDENTIALS", "data/google/credentials.json")
    google_token: str = os.getenv("DORAEMON_GOOGLE_TOKEN", "data/google/token.json")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    # While the web app is open it checks Gmail this often; 0 turns it off ("Run now" still works)
    poll_minutes: int = int(os.getenv("DORAEMON_POLL_MINUTES", "15"))
    # Dorae-2 posts a daily briefing in its chat at this time (while the web app runs); "off" to stop it
    briefing_time: str = os.getenv("DORAEMON_BRIEFING_TIME", "08:00")
    # Telegram bot that sends reminders (from @BotFather); `python -m doraemon.telegram connect` finds your chat
    telegram_token: str = os.getenv("DORAEMON_TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("DORAEMON_TELEGRAM_CHAT_ID", "")  # optional: skips `connect`
    # Reasoning before answering: slower, sometimes more accurate. Off for the per-email pipeline.
    think: bool = os.getenv("DORAEMON_THINK", "false").lower() == "true"
