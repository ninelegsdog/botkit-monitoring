import e2e.client as C
from e2e.client import TelegramTester


def test_get_bot_username(monkeypatch):
    monkeypatch.setattr(C, "bot_getme", lambda t: {"username": "bookingbot"})
    assert TelegramTester.get_bot_username("x") == "bookingbot"
