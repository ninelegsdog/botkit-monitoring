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


def _stub_code(monkeypatch, value: str = "424242") -> None:
    """Answer the code prompt the way the operator does.

    The code is read with a plain input() in client.py, so a test that reaches it without
    this reads pytest's captured stdin and dies with EOFError instead of testing anything.
    """
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: value)


class _FakeClient:
    def __init__(self, authorized: bool, two_factor: bool = False) -> None:
        self._authorized = authorized
        self.two_factor = two_factor
        self.connected = False
        self.disconnected = False
        self.sign_in_calls: list[dict] = []
        self.code_requested = None
        self.code_requests = 0
        # How many password attempts get rejected before one is accepted. telethon re-reads the
        # hash on every sign_in(password=...) call, so a wrong one is free to retry - the test
        # double has to be able to model that, and the 04.10 defect was that nothing did.
        self.passwords_accepted_at = 1
        self._password_attempts = 0
        self.connect_error: Exception | None = None

    async def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def is_user_authorized(self):
        return self._authorized

    async def send_code_request(self, phone):
        self.code_requested = phone
        self.code_requests += 1

    async def sign_in(self, **kw):
        self.sign_in_calls.append(kw)
        # Mirror telethon 1.45 rather than being more forgiving than it. That version types
        # `code` as Union[str, int] and builds the request with `str(code)`; it never calls
        # a callable. A double that accepted a callable silently is why
        # `code=lambda: input(...)` shipped and why every real login failed at
        # PhoneCodeInvalidError with input() never reached.
        if callable(kw.get("code")):
            raise AssertionError(
                "sign_in was handed a callable; telethon 1.45 str()s the code instead of "
                f"calling it, so this would send {str(kw['code'])[:40]!r} as the code"
            )
        if self.two_factor and "password" not in kw:
            # Telethon raises this from sign_in when the account has 2FA and no
            # password was supplied.
            raise client_module.SessionPasswordNeededError(request=None)
        if "password" in kw:
            self._password_attempts += 1
            if self._password_attempts < self.passwords_accepted_at:
                raise client_module.PasswordHashInvalidError(request=None)
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


def test_code_reaches_sign_in_as_a_string(monkeypatch):
    """The bug this file exists to prevent, stated as a contract.

    telethon 1.45 types `code` as Union[str, int] and sends `str(code)`; it does not call a
    callable. Passing `code=lambda: input(...)` therefore sent the callable's repr to
    Telegram: PhoneCodeInvalidError came straight back, the prompt never appeared, and a
    real login could not be completed. Verified by mutation - restoring the lambda fails
    this test.
    """
    _stub_code(monkeypatch, "424242")
    client = _FakeClient(authorized=False)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")

    asyncio.run(t.connect())

    assert len(client.sign_in_calls) == 1, client.sign_in_calls
    code = client.sign_in_calls[0].get("code")
    assert code == "424242", f"sign_in got {code!r} instead of the code the operator typed"
    assert isinstance(code, str)


def test_two_factor_account_is_completed(monkeypatch):
    """The account has 2FA enabled, so this is the path that actually runs.

    Telethon raises SessionPasswordNeededError from sign_in, not from
    send_code_request. The old handler wrapped send_code_request and carried a
    no-cover marker, so the branch was unreachable and the suite stayed green
    while a login with 2FA would have died with a traceback.

    Verified by mutation: deleting the except makes this fail.
    """
    monkeypatch.setattr(client_module.getpass, "getpass", lambda *_: "s3cret")
    _stub_code(monkeypatch)
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
    _stub_code(monkeypatch)
    client = _FakeClient(authorized=False, two_factor=True)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")
    asyncio.run(t.connect())
    assert "2FA" in seen.get("prompt", "")


def test_a_rejected_2fa_password_can_be_retyped_without_a_new_code(monkeypatch):
    """The defect that ended a real login on 04.10.

    Telegram's own mail said it plainly: the code had been entered correctly and the password
    was wrong. The password was asked for once and the exception was not caught, so
    PasswordHashInvalidError ended the run - taking the accepted code with it. The loop around
    this call treats every failure as "request a new code", so the operator would also have been
    told to wait for an SMS that was never needed.

    sign_in(password=...) re-reads the account's password hash on every call and never touches
    the sign-in code, so retrying costs nothing. This pins that property: one code request,
    several password attempts, login completes.
    """
    answers = iter(["wrong-one", "wrong-two", "right-one"])
    monkeypatch.setattr(client_module.getpass, "getpass", lambda *_: next(answers))
    _stub_code(monkeypatch)
    client = _FakeClient(authorized=False, two_factor=True)
    client.passwords_accepted_at = 3  # reject the first two password attempts
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")

    asyncio.run(t.connect())

    assert client.code_requested == "+10000000000", "the code must not be re-requested"
    assert client.code_requests == 1, (
        f"a wrong cloud password must not cost another SMS: {client.code_requests} requests"
    )
    passwords = [c["password"] for c in client.sign_in_calls if "password" in c]
    assert passwords == ["wrong-one", "wrong-two", "right-one"], passwords


def test_a_rejected_2fa_password_says_so_instead_of_raising_a_bare_rpc_error(monkeypatch):
    """Three wrong passwords end the login with a sentence an operator can act on.

    PasswordHashInvalidError carries the text "The password (and thus its hash value) you
    entered is invalid" - true, and useless at 4am: it does not say that the code was fine, that
    no new one is needed, or that the expected value is the account's Two-Step Verification
    password rather than the SMS code.
    """
    monkeypatch.setattr(client_module.getpass, "getpass", lambda *_: "still-wrong")
    _stub_code(monkeypatch)
    client = _FakeClient(authorized=False, two_factor=True)
    client.passwords_accepted_at = 99  # never accept
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(t.connect())

    message = str(excinfo.value)
    assert "not a code problem" in message, message
    assert "Two-Step" in message, message


def test_the_2fa_prompt_names_the_thing_being_asked_for(monkeypatch):
    """Three candidates get confused constantly, and Telegram rejects all of them alike.

    The SMS code is the number that just arrived, the account password is what registered the
    account, and the cloud password is set in Settings -> Privacy -> Two-Step Verification. The
    prompt says which one it wants, because "invalid" does not distinguish them.
    """
    seen = {}

    def fake_getpass(prompt):
        seen["prompt"] = prompt
        return "s3cret"

    monkeypatch.setattr(client_module.getpass, "getpass", fake_getpass)
    _stub_code(monkeypatch)
    client = _FakeClient(authorized=False, two_factor=True)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")
    asyncio.run(t.connect())

    assert "not the SMS code" in seen.get("prompt", ""), seen.get("prompt")


def test_sign_in_without_two_factor_asks_only_once(monkeypatch):
    """No 2FA must not produce a second sign_in call."""
    monkeypatch.setattr(client_module.getpass, "getpass", lambda *_: pytest.fail("password asked"))
    _stub_code(monkeypatch)
    client = _FakeClient(authorized=False)
    t = _tester_with(client)
    t.settings = Settings(api_id=1, api_hash="h", phone="+10000000000")
    asyncio.run(t.connect())
    assert len(client.sign_in_calls) == 1, client.sign_in_calls


def test_connect_signs_in_when_phone_present(monkeypatch):
    _stub_code(monkeypatch)
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
