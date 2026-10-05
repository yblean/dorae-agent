from pathlib import Path

import pytest

from doraemon.config import Settings
from doraemon.email_parse import ParsedEmail, parse_eml

FIXTURES = Path(__file__).parent / "fixtures"


class FakeBackend:
    """Returns a canned response instead of calling a model."""

    name = "fake"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[str] = []

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        self.calls.append(user)
        return self.response


@pytest.fixture
def settings() -> Settings:
    return Settings(timezone="America/New_York", home_currency="USD", model="fake:x", chat_model="off")


@pytest.fixture
def bill_email() -> ParsedEmail:
    return parse_eml((FIXTURES / "bill.eml").read_bytes())
