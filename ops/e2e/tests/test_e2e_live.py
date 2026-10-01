import asyncio
import os

import pytest

from e2e.client import TelegramTester
from e2e.config import load_bots, load_scenarios, load_settings

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_TELEGRAM_E2E"), reason="RUN_TELEGRAM_E2E=1 required")


@pytest.mark.serial
@pytest.mark.parametrize("bot", ["botkit-bookingbot", "botkit-support"])
def test_bot_responds(bot):
    """Live check. Reads usernames from bots.yml, never a bot token."""
    settings = load_settings()
    sc = load_scenarios(settings.scenarios_file)[bot]
    username = load_bots(settings.bots_file)[bot]

    async def go():
        async with TelegramTester(settings) as t:
            t.prime_usernames({bot: username})
            reps = await t.run_scenario(username, sc.steps, settings.timeout)
            return all(e in r for e, r in zip([s.expect for s in sc.steps], reps, strict=False))

    assert asyncio.run(go())
