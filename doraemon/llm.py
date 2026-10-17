"""Model backends. Every step takes a Backend, so models are swappable per step."""
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

import httpx

from doraemon.config import Settings

log = logging.getLogger(__name__)


class Backend(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        """Return a JSON string that should match `schema`."""
        ...


class OllamaBackend:
    def __init__(self, model: str, url: str, think: bool = False) -> None:
        self.model = model
        self.url = url.rstrip("/")
        self.think = think
        self.name = f"ollama:{model}" + (" (thinking)" if think else "")

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        resp = httpx.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": schema,  # Ollama constrains output to this JSON schema
                "stream": False,
                "think": self.think,
                # Ollama defaults to a 4K context, too small for long receipts and forwards
                "options": {"temperature": 0, "num_ctx": 8192},
            },
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """One chat turn that may call tools. Returns Ollama's message: content and maybe tool_calls."""
        resp = httpx.post(
            f"{self.url}/api/chat",
            json={"model": self.model, "messages": messages, "tools": tools, "stream": False,
                  "think": self.think, "options": {"temperature": 0, "num_ctx": 8192}},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["message"]


def ollama_up(url: str, wait: float = 2) -> bool:
    try:
        return httpx.get(f"{url.rstrip('/')}/api/tags", timeout=wait).status_code == 200
    except httpx.HTTPError:
        return False


def ollama_exe() -> Path | None:
    """Ollama's command-line program: on the PATH, or where its Windows installer puts it."""
    found = shutil.which("ollama")
    if found:
        return Path(found)
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return default if default.exists() else None


def model_ready(settings: Settings, spec: str | None = None, wait: float = 30) -> bool:
    """Is the model behind `spec` (default: the email model) reachable? A local Ollama that isn't running is
    started (`ollama serve`, no window) unless DORAEMON_START_OLLAMA is off, waiting up to `wait` seconds."""
    kind, _, _ = (spec or settings.model).partition(":")
    if kind != "ollama" or ollama_up(settings.ollama_url):
        return True
    local = re.match(r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?/?$", settings.ollama_url)
    exe = ollama_exe() if settings.start_ollama and local else None
    if exe is None:
        return False
    log.info("Ollama isn't running: starting %s serve", exe)
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == "nt" \
        else {"start_new_session": True}
    subprocess.Popen([str(exe), "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **hidden)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if ollama_up(settings.ollama_url, wait=1):
            return True
        time.sleep(1)
    return False


def get_backend(spec: str, settings: Settings) -> Backend:
    """`spec` is 'backend:model', e.g. 'ollama:qwen3:4b'."""
    kind, _, model = spec.partition(":")
    if kind == "ollama":
        return OllamaBackend(model, settings.ollama_url, settings.think)
    raise ValueError(f"Unknown backend {kind!r} in {spec!r}")
