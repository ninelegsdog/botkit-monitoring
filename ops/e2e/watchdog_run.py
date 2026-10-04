"""External watchdog: the only component that can report a dead runner.

Runs from its own timer, not from the runner, and stays silent while the runner
is proving it works. One message per outage: a watchdog that re-alerts every
cycle teaches the reader to ignore it, which is the same as not having one.
"""

from __future__ import annotations

import asyncio
import sys
import time

from e2e.client import TelegramTester
from e2e.config import Settings, load_settings
from e2e.heartbeat import alerted_path, classify, read_end_mtime


def compose(verdict) -> str:
    """Triple-quoted on purpose: no backslash escapes to survive being moved."""
    return f"""E2E TestRunnerDead
runner: botkit-e2e (channel: Saved Messages)
{verdict.reason}
the runner cannot report this itself; it stopped reporting"""


async def check(settings: Settings, now: float) -> int:
    verdict = classify(
        read_end_mtime(settings.status_dir), now, settings.watchdog_max_gap_min * 60
    )
    if verdict.state == "ok":
        alerted_path(settings.status_dir).unlink(missing_ok=True)
        print(f"watchdog: ok - {verdict.reason}")
        return 0

    if alerted_path(settings.status_dir).exists():
        print(f"watchdog: already reported - {verdict.reason}")
        return 0

    async with TelegramTester(settings) as t:
        await t.client.send_message("me", compose(verdict))
    # Written only after a confirmed send. A Telegram outage must leave the
    # marker absent, or one failed delivery would silence this watchdog for the
    # whole next outage - the failure mode that hides the failure mode.
    settings.status_dir.mkdir(parents=True, exist_ok=True)
    alerted_path(settings.status_dir).touch()
    print(f"watchdog: reported - {verdict.reason}")
    return 0


def main() -> int:
    return asyncio.run(check(load_settings(), time.time()))


if __name__ == "__main__":
    sys.exit(main())
