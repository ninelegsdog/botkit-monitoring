import asyncio
from typing import ClassVar

from e2e import run_e2e
from e2e.config import Settings

FAKE_BOT = type("S", (), {"steps": [type("St", (), {"send": "/start", "expect": "Привет"})()]})


def _bots_file(tmp_path):
    p = tmp_path / "bots.yml"
    p.write_text("botkit-x: x_test_bot\n")
    return p


def _settings(tmp_path):
    return Settings(
        api_id=1,
        api_hash="h",
        bots_file=_bots_file(tmp_path),
        status_dir=tmp_path / "status",
        session_dir=tmp_path / "session",
        alert_url="http://127.0.0.1:9093/api/v2/alerts",
    )


def _patch(monkeypatch, sent):
    monkeypatch.setattr(run_e2e, "send_alert", lambda s, bot, reason: sent.append(reason) or True)


def test_runner_status(tmp_path, monkeypatch):
    sent: list[str] = []
    _patch(monkeypatch, sent)

    class Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def run_scenario(self, u, steps, t):
            return [s.expect for s in steps]

        def prime_usernames(self, mapping):
            self.prime = mapping

        def username_for(self, bot):
            return self.prime[bot]

    monkeypatch.setattr(run_e2e, "TelegramTester", lambda s: Fake())

    settings = _settings(tmp_path)
    asyncio.run(run_e2e.run_all({"botkit-x": FAKE_BOT}, settings))
    assert (settings.status_dir / "botkit-x.ok").exists()
    assert not (settings.status_dir / "botkit-x.fail").exists()
    assert not (settings.status_dir / ".alerted.botkit-x").exists()
    assert sent == []


def test_runner_fail_writes_fail(tmp_path, monkeypatch):
    sent: list[str] = []
    _patch(monkeypatch, sent)

    class Failing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def run_scenario(self, u, steps, t):
            raise TimeoutError("no reply")

        def prime_usernames(self, mapping):
            self.prime = mapping

        def username_for(self, bot):
            return self.prime[bot]

    monkeypatch.setattr(run_e2e, "TelegramTester", lambda s: Failing())

    settings = _settings(tmp_path)
    problems = asyncio.run(run_e2e.run_all({"botkit-x": FAKE_BOT}, settings))
    assert problems == 1
    assert (settings.status_dir / "botkit-x.fail").exists()
    assert not (settings.status_dir / "botkit-x.ok").exists()
    assert sent == ["no reply"]


def test_runner_reads_username_from_bots_file(tmp_path, monkeypatch):
    """W3: the runner must resolve usernames via bots.yml, never a bot token.

    Mutating username_for back to a token lookup does not break this test by
    itself, so the token-read test in test_no_token_reads.py is the real guard.
    """

    class Fake:
        seen: ClassVar[dict] = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def run_scenario(self, u, steps, t):
            Fake.seen["username"] = u
            return [s.expect for s in steps]

        def prime_usernames(self, mapping):
            Fake.seen["primed"] = dict(mapping)

        def username_for(self, bot):
            return Fake.seen["primed"][bot]

    monkeypatch.setattr(run_e2e, "TelegramTester", lambda s: Fake())
    settings = _settings(tmp_path)
    asyncio.run(run_e2e.run_all({"botkit-x": FAKE_BOT}, settings))
    assert Fake.seen["primed"] == {"botkit-x": "x_test_bot"}
    assert Fake.seen["username"] == "x_test_bot"


def test_send_alert_throttle(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_post(url, payload, timeout=5):
        calls["n"] += 1
        return True

    monkeypatch.setattr(run_e2e, "post_alert", fake_post)
    settings = _settings(tmp_path)
    settings.status_dir.mkdir(parents=True, exist_ok=True)
    assert run_e2e.send_alert(settings, "b", "x") is True
    assert run_e2e.send_alert(settings, "b", "x") is False
    assert calls["n"] == 1


def test_send_alert_uses_settings_url(tmp_path, monkeypatch):
    """D3: the alert URL comes from Settings. A constant here would make
    alert_url a setting that does nothing."""
    seen = {}

    def fake_post(url, payload, timeout=5):
        seen["url"] = url
        return True

    monkeypatch.setattr(run_e2e, "post_alert", fake_post)
    settings = _settings(tmp_path)
    settings.alert_url = "http://10.77.0.1:9093/api/v2/alerts"
    settings.status_dir.mkdir(parents=True, exist_ok=True)
    run_e2e.send_alert(settings, "b", "x")
    assert seen["url"] == "http://10.77.0.1:9093/api/v2/alerts"


def test_send_alert_without_url_is_loud_and_sends_nothing(tmp_path, monkeypatch, capsys):
    """A missing alert URL must not look like a successful alert."""
    calls = {"n": 0}

    def fake_post(url, payload, timeout=5):
        calls["n"] += 1
        return True

    monkeypatch.setattr(run_e2e, "post_alert", fake_post)
    settings = _settings(tmp_path)
    settings.alert_url = ""
    settings.status_dir.mkdir(parents=True, exist_ok=True)
    assert run_e2e.send_alert(settings, "b", "x") is False
    assert calls["n"] == 0
    assert "E2E_ALERT_URL" in capsys.readouterr().out


def test_alert_payload_carries_bot_and_reason():
    payload = run_e2e.build_alert("botkit-x", "boom")
    labels = payload[0]["labels"]
    assert labels["alertname"] == "E2ETestFailed"
    assert labels["severity"] == "warning"
    assert labels["bot"] == "botkit-x"
    assert payload[0]["annotations"]["description"] == "boom"
