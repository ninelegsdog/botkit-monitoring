import os

import pytest
from e2e.client import TelegramTester
from e2e.config import load_scenarios, load_settings

pytestmark = pytest.mark.skipif(not os.environ.get("TG_API_ID"), reason="no e2e creds")


@pytest.mark.parametrize("bot", ["botkit-bookingbot", "botkit-support"])
def test_bot_responds(bot):
    settings = load_settings()
    sc = load_scenarios("scenarios.yml")[bot]
    import asyncio

    username = TelegramTester.get_bot_username(__import__("e2e.run_e2e", fromlist=["x"]).token_for(bot))

    async def go():
        async with TelegramTester(settings) as t:
            reps = await t.run_scenario(username, sc.steps, settings.timeout)
            return all(e in r for e, r in zip([s.expect for s in sc.steps], reps))

    assert asyncio.run(go())
