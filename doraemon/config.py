"""Single-user settings, read from the environment or a .env file.

v0 has one user (us), so these replace the `users` table for now.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[1]  # where .env and data/ live
load_dotenv(PROJECT / ".env")


def in_project(path: str) -> str:
    """Relative paths count from the project folder, not from wherever Doraemon was started: started from
    another folder (say .venv/Scripts), it would otherwise make a second, empty database there."""
    return str(path if Path(path).is_absolute() else PROJECT / path)


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
    db_path: str = in_project(os.getenv("DORAEMON_DB", "data/doraemon.db"))  # your rules; never committed
    # OAuth client from Google Cloud, and the sign-in token saved after you approve access
    google_credentials: str = in_project(os.getenv("DORAEMON_GOOGLE_CREDENTIALS", "data/google/credentials.json"))
    google_token: str = in_project(os.getenv("DORAEMON_GOOGLE_TOKEN", "data/google/token.json"))
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    # While Doraemon runs (service or web app) it checks Gmail this often; 0 turns it off ("Run now" still works)
    poll_minutes: int = int(os.getenv("DORAEMON_POLL_MINUTES", "15"))
    # Dorae-2 posts a daily briefing in its chat at this time (while Doraemon runs); "off" to stop it
    briefing_time: str = os.getenv("DORAEMON_BRIEFING_TIME", "08:00")
    briefing_telegram: bool = os.getenv("DORAEMON_BRIEFING_TELEGRAM", "on").lower() != "off"  # also send it there
    # Dorae-3's daily check (protein, calories, steps left and how to close the gap), also on Telegram; "off" to stop it
    fitness_check_time: str = os.getenv("DORAEMON_FITNESS_CHECK_TIME", "18:00")
    # Where your iPhone's Shortcuts automation saves Apple Health step counts (docs/apple-health-steps.md); several
    # folders separated by ";". By default the places iCloud Drive (iCloud for Windows) and OneDrive put them.
    health_folder: str = os.getenv("DORAEMON_HEALTH_FOLDER", ";".join(str(p) for p in (
        Path.home() / "iCloudDrive" / "Shortcuts" / "Doraemon" / "health",  # a Shortcut's Save File, not asking where
        Path.home() / "iCloudDrive" / "Doraemon" / "health",                # or a folder picked in iCloud Drive
        Path(os.getenv("OneDrive") or Path.home() / "OneDrive") / "Doraemon" / "health")))
    # Send each new suggestion from your email to Telegram with Confirm / Remind me and Dismiss; "off" for the app only
    telegram_new_items: bool = os.getenv("DORAEMON_TELEGRAM_NEW_ITEMS", "on").lower() != "off"
    # Dismiss suggestions still waiting for your OK a day after their date (undo under Activity); "off" keeps them
    expire_passed: bool = os.getenv("DORAEMON_EXPIRE_PASSED", "on").lower() != "off"
    # Start Ollama (ollama serve) when the model is needed and it isn't running; "off" to leave it to you
    start_ollama: bool = os.getenv("DORAEMON_START_OLLAMA", "on").lower() != "off"
    # Keep reminding (every few hours, in waking hours) until you press Done on Telegram; "off" for planned ones only
    remind_until_done: bool = os.getenv("DORAEMON_REMIND_UNTIL_DONE", "on").lower() != "off"
    # Telegram bot that sends reminders (from @BotFather); `python -m doraemon.telegram connect` finds your chat
    telegram_token: str = os.getenv("DORAEMON_TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("DORAEMON_TELEGRAM_CHAT_ID", "")  # optional: skips `connect`
    # Everyday questions ("how much did I spend this week", "my reminders") answered from the data without
    # the model: instant instead of 10-20s. "off" sends every question to the model.
    fast_answers: bool = os.getenv("DORAEMON_FAST_ANSWERS", "on").lower() != "off"
    # Webull SG OpenAPI keys (read-only use: account value and positions for net worth). Apply in Webull first.
    webull_app_key: str = os.getenv("DORAEMON_WEBULL_APP_KEY", "")
    webull_app_secret: str = os.getenv("DORAEMON_WEBULL_APP_SECRET", "")
    webull_endpoint: str = os.getenv("DORAEMON_WEBULL_ENDPOINT", "api.webull.com.sg")
    # Reasoning before answering: slower, sometimes more accurate. Off for the per-email pipeline.
    think: bool = os.getenv("DORAEMON_THINK", "false").lower() == "true"
