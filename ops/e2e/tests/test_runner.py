import asyncio

from e2e import run_e2e
from e2e.config import Settings

FAKE_BOT = type("S", (), {"steps": [type("St", (), {"send": "/start", "expect": "Привет"})()]})


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(run_e2e, "token_for", lambda bot, bots_dir: "dummy-token")
    monkeypatch.setattr(run_e2e, "send_alert", lambda *a, **k: True)


def test_runner_status(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)

    class Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def run_scenario(self, u, steps, t):
            return [s.expect for s in steps]

        @staticmethod
        def get_bot_username(tok):
            return "bot"

    monkeypatch.setattr(run_e2e, "TelegramTester", lambda s: Fake())

    settings = Settings(api_id=1, api_hash="h", phone="+0")
    asyncio.run(run_e2e.run_all({"botkit-x": FAKE_BOT}, settings, tmp_path))
    assert (tmp_path / "botkit-x.ok").exists()
    assert not (tmp_path / "botkit-x.fail").exists()
    assert not (tmp_path / ".alerted.botkit-x").exists()


def test_runner_fail_writes_fail(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)

    class Failing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def run_scenario(self, u, steps, t):
            raise TimeoutError("no reply")

        @staticmethod
        def get_bot_username(tok):
            return "bot"

    monkeypatch.setattr(run_e2e, "TelegramTester", lambda s: Failing())

    settings = Settings(api_id=1, api_hash="h", phone="+0")
    problems = asyncio.run(run_e2e.run_all({"botkit-x": FAKE_BOT}, settings, tmp_path))
    assert problems == 1
    assert (tmp_path / "botkit-x.fail").exists()
    assert not (tmp_path / "botkit-x.ok").exists()


def test_send_alert_throttle(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_post(url, json, timeout):
        calls["n"] += 1
        return type("R", (), {"ok": True})()

    monkeypatch.setattr(run_e2e.requests, "post", fake_post)
    a1 = run_e2e.send_alert("b", "x", tmp_path)
    a2 = run_e2e.send_alert("b", "x", tmp_path)
    assert a1 is True and a2 is False and calls["n"] == 1
