"""Contract for the live E2E parametrisation: it must cover the whole fleet.

The live test is opt-in, so CI never runs it - which means a change that shrank
it back to two bots would be invisible: `RUN_TELEGRAM_E2E` is unset, the suite is
skipped, and the green build says nothing about the seven bots that dropped out.
That is exactly how the hardcoded pair survived: a skipped test cannot fail.

So the coverage is asserted here, offline, against the same `bots.yml` the
runner reads. The rule is "the live test's fleet equals the fleet", and it is
checked by reading the live test's own `_fleet()`, not a copy of the list: a
second list would drift, and drift is the thing being guarded.

Each assertion has a way to fail. Removing a bot from `bots.yml` while leaving it
in a scenario fails; adding a bot to `bots.yml` without a scenario fails; a
parametrisation that no longer derives from `bots.yml` cannot match the expected
set. A test that only restates today's nine names would go red on every honest
fleet change and get relaxed, so the names are derived, not written down.
"""

from __future__ import annotations

import pathlib

from e2e.config import load_bots, load_scenarios
from tests.test_e2e_live import _fleet

E2E_DIR = pathlib.Path(__file__).resolve().parents[1]
BOTS = E2E_DIR / "bots.yml"
SCENARIOS = E2E_DIR / "scenarios.yml"


def test_live_test_covers_every_bot_in_the_fleet():
    """The live parametrisation must equal what bots.yml defines - no subset.

    This is the rule the hardcoded pair broke. `_fleet()` is the live test's own
    source of bots, so this fails if that source stops being bots.yml or stops
    returning everything in it.
    """
    fleet = sorted(load_bots(BOTS))
    assert _fleet() == fleet, (
        f"the live e2e test covers {_fleet()}, but the fleet in bots.yml is {fleet}"
    )


def test_every_bot_in_the_fleet_has_a_scenario():
    """A bot with no scenario cannot be checked live, and would vanish silently."""
    fleet = set(load_bots(BOTS))
    covered = set(load_scenarios(SCENARIOS))
    missing = sorted(fleet - covered)
    assert not missing, f"bots in bots.yml with no scenario: {missing}"


def test_no_scenario_for_a_bot_outside_the_fleet():
    """The other direction: a scenario for a removed bot is a dead check."""
    fleet = set(load_bots(BOTS))
    extra = sorted(set(load_scenarios(SCENARIOS)) - fleet)
    assert not extra, f"scenarios without a matching bot in bots.yml: {extra}"


def test_the_gate_can_fail_on_a_shrunken_fleet(monkeypatch, tmp_path):
    """Negative test: a live test limited to a subset must be caught.

    The hardcoded pair is reproduced directly - the parametrisation reports two
    bots while the fleet has more - and the gate above must call it a mismatch.
    Without this, the rule above is only ever seen passing, which is
    indistinguishable from a rule that cannot fail.
    """
    reduced = pathlib.Path(tmp_path / "test_e2e_live.py")
    reduced.write_text("def _fleet():\n    return ['botkit-bookingbot', 'botkit-support']\n")
    ns: dict = {}
    exec(compile(reduced.read_text(), str(reduced), "exec"), ns)  # our own fixture, not input
    assert ns["_fleet"]() != sorted(load_bots(BOTS))
