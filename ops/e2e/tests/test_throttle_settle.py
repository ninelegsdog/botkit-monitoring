"""Regression guard for the inter-step gap.

W18 reported 3/9 and it was read as six broken /start handlers. The handlers were
fine: the runner sent the next step the moment the previous reply landed, and six
bots run ThrottlingMiddleware(min_interval=2.0), which drops any message that arrives
inside that window by returning None before a handler is called. aiogram still logs
"is handled" for a swallowed update, so the logs looked healthy.

These tests pin the invariant so the gap is decided by SETTLE_S and not by a race
against wall-clock timing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from e2e.client import POLL_S, SETTLE_S, TelegramTester

# The slowest ThrottlingMiddleware across the nine bots. Keep in sync with
# src/core/throttling.py in each bot repository.
FLEET_MAX_THROTTLE_S = 2.0


def test_settle_exceeds_slowest_fleet_throttle() -> None:
    assert SETTLE_S > FLEET_MAX_THROTTLE_S, (
        f"SETTLE_S={SETTLE_S} does not clear the slowest fleet throttle "
        f"({FLEET_MAX_THROTTLE_S}s); bots running min_interval=2.0 will swallow the step"
    )


def test_poll_is_faster_than_settle() -> None:
    assert POLL_S < SETTLE_S, "polling slower than settling reintroduces the race"


class _Client:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def get_messages(self, username: str, limit: int = 1) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=100)]

    async def send_message(self, username: str, text: str) -> None:
        self.sent.append(text)


class _FakeTester(TelegramTester):
    def __init__(self, replies: list[str]) -> None:
        self.client = _Client()
        self._replies = replies

    async def _wait_reply(self, username: str, timeout: int, after_id: int) -> tuple[str, int]:
        return self._replies.pop(0), after_id + 1


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


def test_settles_between_steps_but_not_after_the_last(slept: list[float]) -> None:
    steps = [SimpleNamespace(send="/start"), SimpleNamespace(send="/start")]
    tester = _FakeTester(["first reply", "second reply"])

    replies = asyncio.run(tester.run_scenario("bot", steps, timeout=30))

    assert replies == ["first reply", "second reply"]
    assert tester.client.sent == ["/start", "/start"]
    # Exactly one settle: between the two steps, none after the final reply.
    assert slept == [SETTLE_S]


def test_single_step_scenario_never_settles(slept: list[float]) -> None:
    tester = _FakeTester(["only reply"])

    assert asyncio.run(tester.run_scenario("bot", [SimpleNamespace(send="/start")], timeout=30)) == ["only reply"]
    assert slept == []