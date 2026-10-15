"""W7: the E2E unit and timer are a contract, not configuration.

Both files were rewritten in the sprint plan §D and both encode decisions that
are invisible at runtime: the runner must not be able to read the fleet, the
session must live outside $HOME, and the alert URL must not be a module
constant. A regression here is silent - the unit still starts, the run still
reports OK for the wrong reason.

This is the same approach as test_smoke_schedule.py: no systemd-analyze verify
(which resolves the host's own units and reports every ExecStart as missing),
but real assertions on the directives that actually broke things before.
"""

import pathlib
import re

import pytest
import yaml

from e2e import run_e2e
from e2e.config import Settings

E2E_DIR = pathlib.Path(__file__).resolve().parent.parent
UNIT = (E2E_DIR / "systemd" / "botkit-e2e.service").read_text()
TIMER = (E2E_DIR / "systemd" / "botkit-e2e.timer").read_text()
SCENARIOS = E2E_DIR / "scenarios.yml"

# Ceiling for the whole fleet, not a target. The account is a real user account
# and the cost of being wrong is a ban, so the number is deliberately above the
# current 104 a day - headroom for a scenario or two, not for doubling the fleet.
# A test that only restates today's number would go red on every honest edit and
# get relaxed; this one only fires when the growth is real.
BUDGET_MAX_OUTBOUND_PER_DAY = 150


def _directive(unit: str, key: str) -> list[str]:
    return re.findall(rf"^{re.escape(key)}=(.*)$", unit, re.MULTILINE)


def test_execstart_runs_the_canon_copy():
    """The prod incident: a timer pointed at /root/botkit-e2e/... while the fix
    lived in the repository. A copy cannot keep itself current; canon can."""
    starts = _directive(UNIT, "ExecStart")
    assert len(starts) == 1, f"expected exactly one ExecStart, got {starts}"
    assert starts[0].startswith("/opt/botkit-e2e/"), (
        f"ExecStart must run the canon clone, got {starts[0]!r}"
    )
    assert "/root/" not in UNIT, "the unit must not reference a root-owned copy"


def test_environment_file_is_the_e2e_env():
    envs = _directive(UNIT, "EnvironmentFile")
    assert envs == ["/etc/botkit-e2e/e2e.env"], f"got {envs}"


def test_runs_as_the_dedicated_unprivileged_user():
    users = _directive(UNIT, "User")
    assert users == ["botkit-e2e"], f"got {users}"
    assert _directive(UNIT, "Group") == ["botkit-e2e"]


def test_unit_cannot_reach_the_fleet_source_or_deploy_keys():
    """§E2 condition 2-4: predator hosts the bot repos and the deploy keys, so
    the runner must be unable to read them."""
    inaccessible = _directive(UNIT, "InaccessiblePaths")
    for required in ("/home", "/root"):
        assert required in " ".join(inaccessible), f"{required} must be inaccessible, got {inaccessible}"
    assert _directive(UNIT, "ProtectHome") == ["true"]
    assert _directive(UNIT, "NoNewPrivileges") == ["true"]


def test_unit_drops_every_capability():
    assert _directive(UNIT, "CapabilityBoundingSet") == [""]
    assert _directive(UNIT, "RestrictSUIDSGID") == ["true"]


def test_write_access_is_limited_to_session_and_status():
    """§15: the session is the one thing that must be writable, and status
    markers are the runner's only other output."""
    rw = [p for p in _directive(UNIT, "ReadWritePaths")]
    joined = " ".join(rw)
    assert "/var/lib/botkit-e2e/session" in joined, f"session dir must be writable, got {rw}"
    assert "/var/lib/botkit-e2e/status" in joined, f"status dir must be writable, got {rw}"
    assert _directive(UNIT, "ProtectSystem") == ["strict"]


def test_session_path_is_outside_home():
    """§E2 condition 1, asserted where the decision is made rather than in prose:
    the session directory is absolute and not under /home or /root."""
    s = Settings(api_id=1, api_hash="h")
    path = str(s.session_path)
    assert path.startswith("/var/lib/botkit-e2e/"), path
    assert not path.startswith(("/home/", "/root/")), f"session must not live under $HOME: {path}"


def test_timer_runs_every_six_hours_with_jitter():
    """Strategy B: one dialog per bot per run, not a message every quarter hour.

    The 15-minute cadence was the old value and it multiplied the account volume
    by 24; the exact count lives in the budget test below, derived and checked."""
    oncal = _directive(TIMER, "OnCalendar")
    assert oncal, "OnCalendar is required"
    assert "00/6" in oncal[0], f"expected a 6h cadence, got {oncal}"
    assert ":0/15" not in oncal[0], "the 15-minute cadence is exactly what this work item removed"
    jitter = _directive(TIMER, "RandomizedDelaySec")
    assert jitter, "RandomizedDelaySec is required so runs do not look mechanical"
    assert _directive(TIMER, "Persistent") == ["true"]


def _outbound_budget() -> tuple[int, int, int, int]:
    """Count what a run actually sends, from the scenarios, not from prose.

    A press is an outbound call too: it reaches Telegram as getBotCallbackAnswer,
    so a budget that counted only `send` would understate the traffic and let the
    account drift into a ban while the number still looked safe.
    """
    scenarios = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    sends = sum(1 for sc in scenarios.values() for st in sc["steps"] if "send" in st)
    presses = sum(1 for sc in scenarios.values() for st in sc["steps"] if "press" in st)
    per_run = sends + presses
    hours = int(re.search(r"00/(\d+)", _directive(TIMER, "OnCalendar")[0]).group(1))
    runs_per_day = 24 // hours
    # One summary per run keeps the absence of a message meaningful (watchdog half).
    per_day = per_run * runs_per_day + runs_per_day
    return sends, presses, per_run, per_day


def test_message_budget_is_within_the_ceiling():
    """The account is a user account; the cost of over-sending is a ban."""
    *_, per_day = _outbound_budget()
    assert per_day <= BUDGET_MAX_OUTBOUND_PER_DAY, (
        f"{per_day} outbound calls a day exceeds the {BUDGET_MAX_OUTBOUND_PER_DAY} ceiling"
    )


def test_documented_budget_matches_the_scenarios():
    """The same budget was three different numbers at once: 76/day in the installed
    timer, 96/day in the repository copy and 36/day in a test docstring, none of
    them counting button presses. Prose cannot be trusted to stay derived, so the
    number in the timer comment is asserted against scenarios.yml here."""
    sends, presses, per_run, per_day = _outbound_budget()
    for expected in (
        f"{sends} sends + {presses} button presses",
        f"{per_run} outbound calls per run",
        f"{per_day}/day",
    ):
        assert expected in TIMER, (
            f"timer comment does not state the recomputed budget ({expected!r}); "
            "update ops/e2e/systemd/botkit-e2e.timer"
        )


def test_alert_url_is_not_hardcoded_in_the_runner():
    """D3: a module constant here would make E2E_ALERT_URL a setting that does
    nothing, which is the exact class of bug already fixed twice in this repo."""
    src = (E2E_DIR / "e2e" / "run_e2e.py").read_text()
    assert "AM_URL" not in src, "alert URL must come from Settings, not a module constant"
    assert not hasattr(run_e2e, "AM_URL")


@pytest.mark.parametrize("unit", [UNIT])
def test_unit_file_is_not_empty(unit):
    assert len(unit.splitlines()) > 10
