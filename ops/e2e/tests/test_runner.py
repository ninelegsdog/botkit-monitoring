import asyncio

from e2e import run_e2e
from e2e.config import Settings


def test_runner_status(tmp_path, monkeypatch):
    monkeypatch.setattr(run_e2e, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(run_e2e, "token_for", lambda bot: "dummy-token")

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

    St = type("St", (), {"send": "/start", "expect": "Привет"})
    S = type("S", (), {"steps": [St()]})
    sc = {"botkit-x": S()}

    settings = Settings(api_id=1, api_hash="h", phone="+0")
    asyncio.run(run_e2e.run_all(sc, settings, tmp_path))
    assert (tmp_path / "botkit-x.ok").exists()
    assert not (tmp_path / "botkit-x.fail").exists()
