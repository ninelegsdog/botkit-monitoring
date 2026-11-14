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
import re
import shutil
import subprocess

import pytest

# ops/e2e/tests/test_health_rollout_gate.py -> parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]
HEALTH_SH = REPO / "ops" / "health" / "healthcheck.sh"
ROLLOUT_SH = REPO / "ops" / "rollout" / "check_updates.sh"

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


def test_healthcheck_textfile_is_readable_by_the_collector(run_health: RunHealth) -> None:
    """mktemp under root's umask gives 600, and node-exporter cannot read that.

    The collector runs unprivileged in its own container. A 600 textfile is not a
    cosmetic defect: the metric is simply absent, and the alert guarding it is
    `absent(...) or (time() - metric > 900)`, so the absence itself is the firing
    condition. On 14.11 both self-staleness alerts were red for a day while both
    units were writing every few minutes.
    """
    run_health(STUB_TIMER_STATE="enabled")
    mode = run_health.textfile.stat().st_mode & 0o777
    assert mode == 0o644, f"textfile mode is {oct(mode)}; the collector cannot read it"


def test_healthcheck_has_a_single_publish_block() -> None:
    """Two copies of the same block is how the chmod fix would reach only one of them."""
    source = HEALTH_SH.read_text()
    assert source.count('mv -f "$health_out" "$HEALTH_TEXTFILE"') == 1, (
        "the self-staleness publish block is duplicated"
    )


# A minimal fleet.env: fleet.sh refuses to load when any required variable is empty, and
# the values are never contacted because every network call below is stubbed out.
FLEET_ENV_FIXTURE = """FLEET="botkit"
WEBHOOK_DOMAIN="example.invalid"
WEBHOOK_IP="192.0.2.1"
BASE_URL="https://example.invalid"
PROMETHEUS_URL="http://127.0.0.1:9090"
ALERTMANAGER_URL="http://127.0.0.1:9093"
ALERTMANAGER_ALERTS_URL="http://127.0.0.1:9093/api/v2/alerts"
ALERTMANAGER_ALERTS_ACTIVE_URL="http://127.0.0.1:9093/api/v2/alerts?active=true"
GRAFANA_URL="http://127.0.0.1:3000"
LOKI_URL="http://127.0.0.1:3100"
TEMPO_URL="http://127.0.0.1:3200"
MINIO_URL="http://127.0.0.1:9000"
MINIO_CONSOLE_URL="http://127.0.0.1:9001"
OTLP_HTTP_URL="http://127.0.0.1:4318"
OTLP_GRPC_URL="http://127.0.0.1:4317"
CHANNEL="v0.8.5"
WG_OBSERVE_SUBNET="10.8.0.0/24"
WG_OBSERVE_PROD_IP="10.8.0.2"
WG_OBSERVE_MONITOR_IP="10.8.0.1"
WG_OBSERVE_PORT="51820"
"""

CURL_FAIL_STUB = """#!/usr/bin/env bash
# No GitHub run and no registry answer: every bot takes the "skip" branch.
exit 1
"""


class RunRollout:
    """Callable harness around the real check_updates.sh with only host paths repointed.

    STATE_DIR, LOG and MAINTENANCE_FLAG are absolute production paths, so the script is
    copied into a sandbox tree that keeps ops/rollout and ops/lib side by side - that is
    how the real one resolves fleet.sh and deploy_rollout.sh. Nothing else is rewritten.
    """

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.tmp_path = tmp_path
        self.textfile = tmp_path / "botkit_rollout_check.prom"
        self.log = tmp_path / "rollout.log"
        self.root = tmp_path / "repo"
        script_dir = self.root / "ops" / "rollout"
        lib_dir = self.root / "ops" / "lib"
        script_dir.mkdir(parents=True)
        lib_dir.mkdir(parents=True)
        shutil.copy(REPO / "ops" / "lib" / "fleet.sh", lib_dir / "fleet.sh")
        rollout = script_dir / "deploy_rollout.sh"
        rollout.write_text("#!/usr/bin/env bash\nexit 0\n")
        rollout.chmod(0o755)
        self.fleet_env = tmp_path / "fleet.env"
        self.fleet_env.write_text(FLEET_ENV_FIXTURE)

        source = ROLLOUT_SH.read_text()
        rewrites = {
            "STATE_DIR=": f'STATE_DIR="{tmp_path / "state"}"',
            "LOG=": f'LOG="{self.log}"',
            "MAINTENANCE_FLAG=": f'MAINTENANCE_FLAG="{tmp_path / "maintenance"}"',
        }
        for prefix, replacement in rewrites.items():
            source, replaced = re.subn(
                rf"(?m)^{re.escape(prefix)}.*$", lambda _m, r=replacement: r, source
            )
            assert replaced == 1, f"check_updates.sh no longer defines {prefix} once"
        self.script = script_dir / "check_updates.sh"
        self.script.write_text(source)

        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        for name, body in (("curl", CURL_FAIL_STUB), ("docker", DOCKER_STUB)):
            path = stub_bin / name
            path.write_text(body)
            path.chmod(0o755)

    def __call__(self) -> str:
        subprocess.run(
            ["bash", str(self.script)],
            env={
                **os.environ,
                "PATH": f"{self.tmp_path / 'bin'}:{os.environ['PATH']}",
                "BOTKIT_FLEET_ENV": str(self.fleet_env),
                "HEALTH_CHECK_TEXTFILE": str(self.textfile),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture
def run_rollout(tmp_path: pathlib.Path) -> RunRollout:
    return RunRollout(tmp_path)


def test_rollout_textfile_is_readable_by_the_collector(run_rollout: RunRollout) -> None:
    """Same defect as healthcheck.sh, same consequence: a missing metric fires the alert."""
    run_rollout()
    assert run_rollout.textfile.exists(), run_rollout.log
    mode = run_rollout.textfile.stat().st_mode & 0o777
    assert mode == 0o644, f"textfile mode is {oct(mode)}; the collector cannot read it"
    assert "botkit_rollout_check_last_run_seconds " in run_rollout.textfile.read_text()
