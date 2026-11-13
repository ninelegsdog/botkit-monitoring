"""The healthcheck must report a disabled auto-rollout timer.

A stopped rollout timer freezes the fleet silently: merges keep building images,
nothing deploys them, and every bot keeps answering 200 on an old tag while all
dashboards read green. That is not hypothetical - the timer was stopped by hand
on 07.11 during maintenance and left off, and the next merge would have gone out
nowhere. Nothing in the checks noticed.

The gate is tested against the shipped script with stubbed `curl`, `docker` and
`systemctl`, so the assertion cannot be satisfied by a copy of the logic: it says
that a disabled unit produces a FAIL line, an enabled one does not, and a missing
unit is its own message rather than a silent pass.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

# ops/e2e/tests/test_health_rollout_gate.py -> parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]
HEALTH_SH = REPO / "ops" / "health" / "healthcheck.sh"

SYSTEMCTL_STUB = """#!/usr/bin/env bash
# Only the three call shapes healthcheck.sh uses.
case "$1" in
  --failed) exit 0 ;;
  cat) exit "${STUB_TIMER_CAT_RC:-0}" ;;
  is-enabled) echo "${STUB_TIMER_STATE:-enabled}" ;;
  *) exit 0 ;;
esac
"""

CURL_STUB = """#!/usr/bin/env bash
echo 200
"""

DOCKER_STUB = """#!/usr/bin/env bash
exit 0
"""


class RunHealth:
    """Callable harness around the real healthcheck.sh with stubbed systemctl/curl/docker."""

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.tmp_path = tmp_path
        self.log = tmp_path / "health.log"
        self.textfile = tmp_path / "botkit_healthcheck.prom"
        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        for name, body in (
            ("systemctl", SYSTEMCTL_STUB),
            ("curl", CURL_STUB),
            ("docker", DOCKER_STUB),
        ):
            path = stub_bin / name
            path.write_text(body)
            path.chmod(0o755)

    def __call__(self, **env: str) -> str:
        subprocess.run(
            ["bash", str(HEALTH_SH)],
            env={
                **os.environ,
                "PATH": f"{self.tmp_path / 'bin'}:{os.environ['PATH']}",
                "BOTKIT_HEALTH_LOG": str(self.log),
                "BOTKIT_HEALTH_TEXTFILE": str(self.textfile),
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture
def run_health(tmp_path: pathlib.Path) -> RunHealth:
    """Run the real healthcheck with stubbed systemctl/curl/docker."""
    return RunHealth(tmp_path)


def test_disabled_rollout_timer_is_reported(run_health) -> None:
    log = run_health(STUB_TIMER_STATE="disabled")
    assert "rollout=timer-disabled" in log, log


def test_missing_timer_unit_is_its_own_message(run_health) -> None:
    log = run_health(STUB_TIMER_CAT_RC="1")
    assert "rollout=timer-missing" in log, log


def test_enabled_rollout_timer_is_not_flagged(run_health) -> None:
    """The other direction: the gate must not fail a healthy timer.

    Without this the test would pass if the check always fired.
    """
    log = run_health(STUB_TIMER_STATE="enabled")
    assert "rollout=" not in log, log


def test_healthcheck_publishes_own_staleness(run_health: RunHealth) -> None:
    """The watcher publishes its own freshness; a stale file must FAIL."""
    log = run_health(STUB_TIMER_STATE="enabled")
    assert run_health.textfile.exists(), run_health.textfile
    content = run_health.textfile.read_text()
    assert content.startswith("botkit_healthcheck_last_run_seconds "), content
    assert "botkit_healthcheck_ok " in content, content
    assert "healthcheck=stale" not in log, log


def test_healthcheck_ok_carries_verdict(run_health: RunHealth) -> None:
    """Disabled timer -> ok=0 published alongside the FAIL line."""
    log = run_health(STUB_TIMER_STATE="disabled")
    content = run_health.textfile.read_text()
    assert "botkit_healthcheck_ok 0" in content, content
    assert "rollout=timer-disabled" in log, log
