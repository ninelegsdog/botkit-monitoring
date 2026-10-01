import asyncio

import pytest

import e2e.client as client_module
from e2e.client import TelegramTester
from e2e.config import Settings


def _settings() -> Settings:
    return Settings(api_id=1, api_hash="h")


def test_username_for_returns_primed_value():
    t = TelegramTester.__new__(TelegramTester)
    t._usernames = {}
    t.prime_usernames({"botkit-bookingbot": "bookingbot_test_bot"})
    assert t.username_for("botkit-bookingbot") == "bookingbot_test_bot"


def test_username_for_unknown_bot_raises():
    t = TelegramTester.__new__(TelegramTester)
    t._usernames = {}
    with pytest.raises(RuntimeError, match=r"bots\.yml"):
        t.username_for("botkit-store")


def test_constructor_passes_session_labels(monkeypatch):
    """W2: session metadata must reach TelegramClient, not be dropped on the floor."""
    seen = {}

    def fake_client(session, api_id, api_hash, **kw):
        seen.update({"session": session, "api_id": api_id, "api_hash": api_hash, **kw})
        return object()

    monkeypatch.setattr(client_module, "TelegramClient", fake_client)
    s = Settings(
        api_id=7,
        api_hash="hash",
        session_dir="/var/lib/botkit-e2e/session",
        session_name="botkit-e2e",
        device_model="lab-box",
        system_version="Debian",
        app_version="botkit-e2e/9.9",
    )
    TelegramTester(s)
    assert seen["session"] == "/var/lib/botkit-e2e/session/botkit-e2e.session"
    assert seen["device_model"] == "lab-box"
    assert seen["system_version"] == "Debian"
    assert seen["app_version"] == "botkit-e2e/9.9"


class _FakeClient:
    def __init__(self, authorized: bool) -> None:
        self._authorized = authorized
        self.connected = False
        self.disconnected = False
        self.sign_in_calls: list[dict] = []
        self.code_requested = None

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self._authorized

    async def send_code_request(self, phone):
        self.code_requested = phone

    async def sign_in(self, **kw):
        self.sign_in_calls.append(kw)
        self._authorized = True

    async def disconnect(self):
        self.disconnected = True


def _tester_with(client) -> TelegramTester:
    t = TelegramTester.__new__(TelegramTester)
    t.client = client
    t.settings = _settings()
    return t


def test_connect_accepts_already_authorized_session():
    """W2: an authorized session must not need a phone number."""
    client = _FakeClient(authorized=True)
    t = _tester_with(client)
    asyncio.run(t.connect())
    assert client.connected
    assert client.code_requested is None


def test_connect_raises_when_unauthorized_and_no_phone():
    """W2: the fail-fast contract - RuntimeError now, not a hang in the first scenario.

    The reason this test matters: with the guard removed the run proceeds and
    times out per bot, which is indistinguishable from "the bots are broken".
    """
    client = _FakeClient(authorized=False)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="")
    with pytest.raises(RuntimeError, match="not authorized"):
        asyncio.run(t.connect())
    assert client.sign_in_calls == []


def test_connect_signs_in_when_phone_present():
    client = _FakeClient(authorized=False)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")
    asyncio.run(t.connect())
    assert client.code_requested == "+10000000000"
    assert len(client.sign_in_calls) == 1


def test_disconnect_closes_client():
    client = _FakeClient(authorized=True)
    t = _tester_with(client)
    asyncio.run(t.disconnect())
    assert client.disconnected
