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
    date_order: str = os.getenv("DORAEMON_DATE_ORDER", "DMY")  # how to read 02/10/26
    model: str = os.getenv("DORAEMON_MODEL", "ollama:qwen3:4b")
    db_path: str = os.getenv("DORAEMON_DB", "data/doraemon.db")  # your rules; never committed
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    # Reasoning before answering: slower, sometimes more accurate. Off for the per-email pipeline.
    think: bool = os.getenv("DORAEMON_THINK", "false").lower() == "true"
