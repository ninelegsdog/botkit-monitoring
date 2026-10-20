"""Live E2E check for every bot in the fleet, not the two that were listed.

The check drives a real user account against the production bots and asserts
that each reply contains the expected substring. It is opt-in (`RUN_TELEGRAM_E2E`)
because it needs a live Telegram session; the gate that keeps it honest is
offline and always runs.

Why the whole fleet and not a sample. The version this replaces hardcoded two
bots - bookingbot and support - so seven bots were never exercised live while
the file's own name claimed an e2e check. That is the failure mode this file
exists to prevent: a test that covers a fixed subset reads as "the fleet is
tested" and is not. The list below is derived from `bots.yml`, the same file the
runner reads, so a bot added to the fleet is picked up here without an edit and
a bot removed cannot leave a stale entry behind.

The membership of the fleet is not asserted from a second hardcoded list: it is
cross-checked against `scenarios.yml` by `test_e2e_live_contract.py`, which runs
offline. A parametrisation that quietly shrank to two bots would fail there.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from e2e.client import TelegramTester
from e2e.config import load_bots, load_scenarios, load_settings

E2E_DIR = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_TELEGRAM_E2E"), reason="RUN_TELEGRAM_E2E=1 required"
)


def _fleet() -> list[str]:
    """Every bot in the fleet, read from the file the runner reads.

    Sorted so the parametrisation is stable across runs and readable in the
    report. Absent file is not a silent empty list: `load_bots` raises, and the
    contract test below turns that into a red rather than a skipped suite.
    """
    return sorted(load_bots(E2E_DIR / "bots.yml"))


@pytest.mark.serial
@pytest.mark.parametrize("bot", _fleet())
def test_bot_responds(bot):
    """Live check. Reads usernames from bots.yml, never a bot token."""
    settings = load_settings()
    sc = load_scenarios(settings.scenarios_file)[bot]
    username = load_bots(settings.bots_file)[bot]

    async def go():
        async with TelegramTester(settings) as t:
            t.prime_usernames({bot: username})
            reps = await t.run_scenario(username, sc.steps, settings.timeout)
            return all(
                e in r for e, r in zip([s.expect for s in sc.steps], reps, strict=False)
            )

    assert asyncio.run(go())
