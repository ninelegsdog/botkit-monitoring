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
    def __init__(self, authorized: bool, two_factor: bool = False) -> None:
        self._authorized = authorized
        self.two_factor = two_factor
        self.connected = False
        self.disconnected = False
        self.sign_in_calls: list[dict] = []
        self.code_requested = None
        self.connect_error: Exception | None = None

    async def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def is_user_authorized(self):
        return self._authorized

    async def send_code_request(self, phone):
        self.code_requested = phone

    async def sign_in(self, **kw):
        self.sign_in_calls.append(kw)
        if self.two_factor and "password" not in kw:
            # Telethon raises this from sign_in when the account has 2FA and no
            # password was supplied.
            raise client_module.SessionPasswordNeededError(request=None)
        self._authorized = True

    async def disconnect(self):
        self.disconnected = True


def _tester_with(client) -> TelegramTester:
    t = TelegramTester.__new__(TelegramTester)
    t.client = client
    t.settings = _settings()
    return t


def test_proxy_tuple_absent_for_empty_spec():
    assert client_module._proxy_tuple("") is None


def test_proxy_tuple_parses_socks5():
    assert client_module._proxy_tuple("socks5:127.0.0.1:11080") == ("socks5", "127.0.0.1", 11080)
    assert client_module._proxy_tuple("socks5:10.8.1.2:1080") == ("socks5", "10.8.1.2", 1080)


def test_proxy_tuple_rejects_socks5h_because_telethon_cannot_use_it():
    """socks5h is a curl convention; telethon has no such protocol.

    Found on the live host: "socks5h:..." passed every unit test and then failed
    inside telethon's connect path with "Unknown proxy protocol type", which
    looks like a network fault rather than a setting mistake.
    """
    with pytest.raises(ValueError, match="socks5"):
        client_module._proxy_tuple("socks5h:127.0.0.1:11080")


def test_parsed_protocol_is_one_telethon_accepts():
    """Keep the parser honest against telethon's accepted set rather than my memory of it."""
    accepted = {"http", "https", "socks4", "socks5"}
    parsed = client_module._proxy_tuple("socks5:127.0.0.1:11080")
    assert parsed is not None
    assert parsed[0] in accepted


@pytest.mark.parametrize("spec", ["127.0.0.1:1080", "http://127.0.0.1:1080", "socks5:host", "socks5:host:abc"])
def test_proxy_tuple_rejects_malformed_spec(spec):
    """A typo must fail here, not silently produce a direct connection attempt.

    Falling back to a direct connection would present as "the proxy is broken"
    when the setting is the cause.
    """
    with pytest.raises(ValueError, match="E2E_PROXY"):
        client_module._proxy_tuple(spec)


def test_constructor_passes_the_proxy_through(monkeypatch):
    """The E2E host egress is blackholed by Telegram, so the runner only reaches the
    DC through the loopback SOCKS proxy. If the proxy were dropped here, the run
    would fail as a connection timeout with no hint of the cause."""
    seen = {}

    def fake_client(session, api_id, api_hash, **kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(client_module, "TelegramClient", fake_client)
    TelegramTester(Settings(api_id=1, api_hash="h", proxy="socks5:127.0.0.1:11080"))
    assert seen["proxy"] == ("socks5", "127.0.0.1", 11080)


def test_constructor_without_proxy_passes_none(monkeypatch):
    seen = {}
    monkeypatch.setattr(client_module, "TelegramClient", lambda *a, **kw: seen.update(kw))
    TelegramTester(Settings(api_id=1, api_hash="h"))
    assert seen["proxy"] is None


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


def test_connection_failure_names_the_tunnel(monkeypatch):
    """A dead tunnel and a blocked egress produce the same Telethon ConnectionError.

    Found by stopping the tunnel on the live host: the runner said only
    "Connection to Telegram failed 5 time(s)", which is exactly what the blocked
    IP produced before the tunnel existed. The operator cannot tell a dead
    systemd unit from an unfixable network fact.
    """
    client = _FakeClient(authorized=False)
    client.connect_error = ConnectionError("Connection to Telegram failed 5 time(s)")
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", proxy="socks5:127.0.0.1:11080")
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(t.connect())
    message = str(excinfo.value)
    assert "socks5:127.0.0.1:11080" in message
    assert "botkit-e2e-tunnel" in message


def test_connection_failure_without_proxy_names_the_real_fact():
    client = _FakeClient(authorized=False)
    client.connect_error = ConnectionError("Connection to Telegram failed 5 time(s)")
    t = _tester_with(client)
    with pytest.raises(RuntimeError, match="directly"):
        asyncio.run(t.connect())


def test_two_factor_account_is_completed(monkeypatch):
    """The account has 2FA enabled, so this is the path that actually runs.

    Telethon raises SessionPasswordNeededError from sign_in, not from
    send_code_request. The old handler wrapped send_code_request and carried a
    no-cover marker, so the branch was unreachable and the suite stayed green
    while a login with 2FA would have died with a traceback.

    Verified by mutation: deleting the except makes this fail.
    """
    monkeypatch.setattr(client_module.getpass, "getpass", lambda *_: "s3cret")
    client = _FakeClient(authorized=False, two_factor=True)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")

    asyncio.run(t.connect())

    assert len(client.sign_in_calls) == 2, client.sign_in_calls
    assert "code" in client.sign_in_calls[0], "the first call must try the code alone"
    assert "password" not in client.sign_in_calls[0], client.sign_in_calls[0]
    assert client.sign_in_calls[1] == {"password": "s3cret"}, client.sign_in_calls[1]


def test_two_factor_password_is_not_echoed(monkeypatch):
    """The second factor is a secret; a plain input() would print it on screen."""
    seen = {}

    def fake_getpass(prompt):
        seen["prompt"] = prompt
        return "s3cret"

    monkeypatch.setattr(client_module.getpass, "getpass", fake_getpass)
    client = _FakeClient(authorized=False, two_factor=True)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")
    asyncio.run(t.connect())
    assert "2FA" in seen.get("prompt", "")


def test_sign_in_without_two_factor_asks_only_once(monkeypatch):
    """No 2FA must not produce a second sign_in call."""
    monkeypatch.setattr(client_module.getpass, "getpass", lambda *_: pytest.fail("password asked"))
    client = _FakeClient(authorized=False)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")
    asyncio.run(t.connect())
    assert len(client.sign_in_calls) == 1, client.sign_in_calls


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
