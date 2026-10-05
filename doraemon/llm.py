"""Model backends. Every step takes a Backend, so models are swappable per step."""
from typing import Protocol

import httpx

from doraemon.config import Settings


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


def get_backend(spec: str, settings: Settings) -> Backend:
    """`spec` is 'backend:model', e.g. 'ollama:qwen3:4b'."""
    kind, _, model = spec.partition(":")
    if kind == "ollama":
        return OllamaBackend(model, settings.ollama_url, settings.think)
    raise ValueError(f"Unknown backend {kind!r} in {spec!r}")
