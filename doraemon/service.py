"""Doraemon in the background: checks Gmail, sends Telegram reminders, answers the bot and posts the
morning briefing without the web app open.

    python -m doraemon.service            # run here until Ctrl+C (to try it out)
    python -m doraemon.service install    # start hidden every time you log into Windows
    python -m doraemon.service start      # start hidden now, without logging out
    python -m doraemon.service status     # is it running, and is it set to start at login?
    python -m doraemon.service stop       # stop the hidden one
    python -m doraemon.service uninstall  # stop starting at login

Only one program runs the jobs (see doraemon.jobs): with the service running, the web app is just
for looking and pressing buttons. The laptop sleeping pauses everything; on waking it catches up
(late reminders still go out while they matter, and Dorae-2 says which ones it couldn't send).
What it does is logged to data/service.log.
"""
import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from doraemon.config import Settings
from doraemon.jobs import OWNER_KEY, SERVICE_KEY, FileLock, Runtime

PROJECT = Path(__file__).resolve().parents[1]  # where .env and data/ are
STARTUP_NAME = "Doraemon.vbs"


def startup_file() -> Path:
    """The launcher in your Windows Startup folder (runs at login, no admin rights needed)."""
    return Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup" / STARTUP_NAME


def hidden_python() -> Path:
    """pythonw.exe from the same environment: runs without a console window."""
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return quiet if quiet.exists() else exe


def launcher_script() -> str:
    """A tiny VBScript that starts the service hidden, from the project folder (so .env and data/ are found)."""
    def q(s):
        return str(s).replace('"', '""')
    return ('Set shell = CreateObject("WScript.Shell")\r\n'
            f'shell.CurrentDirectory = "{q(PROJECT)}"\r\n'
            f'shell.Run """{q(hidden_python())}"" -m doraemon.service", 0, False\r\n')


class OnlyProblems(logging.Filter):
    """Webull's SDK logs every step of its session check at INFO and sets its own levels: keep its warnings only."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("webull") or record.levelno >= logging.WARNING


class HideSecrets(logging.Filter):
    """Masks the bot token in anything logged: Telegram's API puts it in every request URL."""

    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if any(s in text for s in self.secrets):
            for s in self.secrets:
                text = text.replace(s, "<token>")
            record.msg, record.args = text, None
        return True


def log_to_file(settings: Settings) -> Path:
    path = Path(settings.db_path).parent / "service.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers = [handler] + ([logging.StreamHandler()] if sys.stderr is not None else [])  # pythonw has no console
    root = logging.getLogger()
    for h in handlers:
        h.addFilter(HideSecrets(settings.telegram_token, settings.webull_app_key, settings.webull_app_secret))
        h.addFilter(OnlyProblems())
        root.addHandler(h)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "webull"):  # one line per request (the token is in Telegram's URLs)
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def run(settings: Settings) -> None:
    path = log_to_file(settings)
    rt = Runtime(settings)
    stop = rt.supervise("background service")
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    print(f"Doraemon's background service is running (log: {path}). Press Ctrl+C to stop.")
    try:
        while not done.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    stop.set()
    logging.info("stopped")


def status(settings: Settings) -> str:
    rt = Runtime(settings)
    owner = rt.owner()
    lines = []
    if owner:
        seen = datetime.fromisoformat(owner["seen"])
        ago = int((datetime.now(timezone.utc) - seen).total_seconds())
        lines.append(f"Jobs are running in the {owner['by']} (pid {owner['pid']}), last heard from {ago}s ago.")
    else:
        lock = FileLock(Path(settings.db_path).parent / "jobs.lock")
        free = lock.acquire()
        lock.release()
        lines.append("Nothing is running Doraemon's jobs: no reminders or briefing will go out." if free else
                     "Something holds the jobs lock but hasn't reported in yet: it's probably just starting.")
    lines.append(f"Starts at login: {'yes' if startup_file().exists() else 'no'} ({startup_file()})")
    return "\n".join(lines)


def start_hidden() -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(hidden_python()), "-m", "doraemon.service"], cwd=PROJECT, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m doraemon.service")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "install", "uninstall", "start", "stop", "status"])
    args = ap.parse_args()
    settings = Settings()
    if args.command == "run":
        return run(settings)
    if args.command == "status":
        return print(status(settings))
    if args.command in ("install", "uninstall") and os.name != "nt":
        sys.exit("Starting at login is set up for Windows only. Elsewhere, run python -m doraemon.service "
                 "from your login items or a systemd user service.")
    if args.command == "install":
        startup_file().write_text(launcher_script(), encoding="utf-8")
        print(f"Doraemon will start in the background when you log in ({startup_file()}).")
        print("To start it now as well: python -m doraemon.service start")
    elif args.command == "uninstall":
        startup_file().unlink(missing_ok=True)
        print("Doraemon won't start at login any more. Stop the running one with: python -m doraemon.service stop")
    elif args.command == "start":
        rt = Runtime(settings)
        if rt.service_running() or (rt.owner() or {}).get("by") == "background service":
            sys.exit("The background service is already running. To restart it: python -m doraemon.service stop, "
                     "then start.")
        start_hidden()
        print("Started. Check with: python -m doraemon.service status")
    elif args.command == "stop":
        owner = Runtime(settings).owner()
        if not owner or owner["by"] != "background service":
            sys.exit("The background service isn't running.")
        os.kill(int(owner["pid"]), signal.SIGTERM)
        rt = Runtime(settings)  # it can't say goodbye when killed: clear its notes so `start` works right away
        rt.db.set_setting(SERVICE_KEY, None)
        rt.db.set_setting(OWNER_KEY, None)
        print(f"Stopped the background service (pid {owner['pid']}).")


if __name__ == "__main__":
    main()
