"""The sync must install and verify the unit files the host actually runs.

Every script in canon was kept equal to the host and the unit files were not. On 14.11 the
installed `botkit-rollout-check.service` still had the `ReadWritePaths` list from before the
textfile metric existed: the poller checked all nine bots and then died on
`mktemp: Read-only file system` when it published its verdict. `BotkitRolloutCheckStale`
fires on `absent(...)`, so the alert read exactly like a dead poller — the unit was broken in
the one way its own alert could not describe.

The tests run the shipped script against a temporary unit directory with `systemctl` and
`systemd-analyze` stubs, so they assert what the script does to the host: which files it
replaces, that a replaced file is backed up, that one daemon-reload happens for the batch,
that a changed active timer is restarted and an inactive one is not, and that `--check`
changes nothing while still failing.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

# ops/e2e/tests/test_unit_install_contract.py -> parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]
INSTALL_SH = REPO / "ops" / "lib" / "install-units.sh"
MANIFEST = REPO / "ops" / "lib" / "managed-units.txt"

SYSTEMCTL_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_SYSTEMCTL_LOG"
case "$1" in
  is-active) [ "${STUB_TIMER_ACTIVE:-1}" = 1 ] && exit 0 || exit 3 ;;
  daemon-reload) exit 0 ;;
  restart) exit 0 ;;
esac
exit 0
"""

ANALYZE_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_ANALYZE_LOG"
[ "${STUB_ANALYZE_FAIL:-0}" = 1 ] && exit 1
exit 0
"""


class Harness:
    """A fake host: one canon tree, one /etc/systemd/system, stubbed systemctl."""

    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.tmp_path = tmp_path
        self.repo = tmp_path / "repo"
        self.dest = tmp_path / "etc-systemd"
        self.dest.mkdir()
        self.systemctl_log = tmp_path / "systemctl.log"
        self.analyze_log = tmp_path / "analyze.log"

        (self.repo / "ops" / "health" / "systemd").mkdir(parents=True)
        (self.repo / "ops" / "drift" / "systemd").mkdir(parents=True)
        self.health = self.repo / "ops" / "health" / "systemd" / "botkit-healthcheck.service"
        self.timer = self.repo / "ops" / "health" / "systemd" / "botkit-healthcheck.timer"
        self.health.write_text("[Service]\nExecStart=/bin/true\n")
        self.timer.write_text("[Timer]\nOnCalendar=daily\n")

        self.manifest = tmp_path / "managed-units.txt"
        self.manifest.write_text("botkit-healthcheck.service\nbotkit-healthcheck.timer\n")

        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        for name, body in (("systemctl", SYSTEMCTL_STUB), ("systemd-analyze", ANALYZE_STUB)):
            path = stub_bin / name
            path.write_text(body)
            path.chmod(0o755)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(INSTALL_SH), *args],
            env={
                **os.environ,
                "PATH": f"{self.tmp_path / 'bin'}:{os.environ['PATH']}",
                "REPO": str(self.repo),
                "UNIT_DEST": str(self.dest),
                "UNIT_MANIFEST": str(self.manifest),
                "UNIT_STAMP": "TESTSTAMP",
                "STUB_SYSTEMCTL_LOG": str(self.systemctl_log),
                "STUB_ANALYZE_LOG": str(self.analyze_log),
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def installed(self, name: str) -> pathlib.Path:
        return self.dest / name


@pytest.fixture
def host(tmp_path: pathlib.Path) -> Harness:
    return Harness(tmp_path)


def test_a_stale_unit_is_replaced_with_a_backup(host: Harness) -> None:
    host.installed("botkit-healthcheck.service").write_text("[Service]\nExecStart=/old\n")
    host.installed("botkit-healthcheck.timer").write_text(host.timer.read_text())

    result = host.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert host.installed("botkit-healthcheck.service").read_text() == host.health.read_text()
    backup = host.dest / "botkit-healthcheck.service.bak-TESTSTAMP"
    assert backup.read_text() == "[Service]\nExecStart=/old\n", "the replaced unit was not kept"


def test_one_daemon_reload_for_the_batch(host: Harness) -> None:
    """Without the reload systemd keeps running the definition that was just replaced."""
    host.installed("botkit-healthcheck.service").write_text("[Service]\nExecStart=/old\n")
    host.installed("botkit-healthcheck.timer").write_text("[Timer]\nOnCalendar=hourly\n")

    host.run()

    log = host.systemctl_log.read_text()
    assert log.count("daemon-reload") == 1, log


def test_a_changed_active_timer_is_restarted(host: Harness) -> None:
    """systemd caches the calendar spec at activation: reload alone keeps the old schedule."""
    host.installed("botkit-healthcheck.service").write_text(host.health.read_text())
    host.installed("botkit-healthcheck.timer").write_text("[Timer]\nOnCalendar=hourly\n")

    host.run()

    log = host.systemctl_log.read_text()
    assert "restart botkit-healthcheck.timer" in log, log


def test_an_inactive_timer_is_not_started(host: Harness) -> None:
    """Restarting a stopped timer would enable work the operator deliberately turned off."""
    host.installed("botkit-healthcheck.service").write_text(host.health.read_text())
    host.installed("botkit-healthcheck.timer").write_text("[Timer]\nOnCalendar=hourly\n")

    host.run(STUB_TIMER_ACTIVE="0")

    log = host.systemctl_log.read_text()
    assert "restart botkit-healthcheck.timer" not in log, log


def test_identical_units_touch_nothing(host: Harness) -> None:
    host.installed("botkit-healthcheck.service").write_text(host.health.read_text())
    host.installed("botkit-healthcheck.timer").write_text(host.timer.read_text())

    result = host.run()

    assert result.returncode == 0
    assert not host.systemctl_log.exists(), "an unchanged host does not need a reload"
    assert not list(host.dest.glob("*.bak-TESTSTAMP"))


def test_check_reports_drift_and_changes_nothing(host: Harness) -> None:
    stale = "[Service]\nExecStart=/old\n"
    host.installed("botkit-healthcheck.service").write_text(stale)
    host.installed("botkit-healthcheck.timer").write_text(host.timer.read_text())

    result = host.run("--check")

    assert result.returncode == 1
    assert "DRIFT" in result.stdout
    assert host.installed("botkit-healthcheck.service").read_text() == stale
    assert not list(host.dest.glob("*.bak-TESTSTAMP"))


def test_check_passes_when_everything_matches(host: Harness) -> None:
    host.installed("botkit-healthcheck.service").write_text(host.health.read_text())
    host.installed("botkit-healthcheck.timer").write_text(host.timer.read_text())

    assert host.run("--check").returncode == 0


def test_a_unit_canon_does_not_ship_is_fatal(host: Harness) -> None:
    """A typo in the manifest must not be read as "nothing to do"."""
    host.manifest.write_text("botkit-healthcheck.service\nbotkit-nonexistent.service\n")

    result = host.run()

    assert result.returncode == 1
    assert "canon ships no such unit" in result.stderr


def test_a_unit_canon_ships_twice_is_fatal(host: Harness) -> None:
    """Two candidates for one basename make "the canon file" a guess."""
    (host.repo / "ops" / "drift" / "systemd" / "botkit-healthcheck.service").write_text("x\n")
    host.installed("botkit-healthcheck.service").write_text("[Service]\nExecStart=/old\n")

    result = host.run()

    assert result.returncode == 1
    assert "ships botkit-healthcheck.service twice" in result.stderr


def test_a_unit_that_fails_verification_is_not_installed(host: Harness) -> None:
    stale = "[Service]\nExecStart=/old\n"
    host.installed("botkit-healthcheck.service").write_text(stale)

    result = host.run(STUB_ANALYZE_FAIL="1")

    assert result.returncode == 1
    assert host.installed("botkit-healthcheck.service").read_text() == stale
    assert not list(host.dest.glob("*.bak-TESTSTAMP"))


def test_an_absent_unit_is_reported_not_installed(host: Harness) -> None:
    """Provisioning is host-setup's job; a unit that was never enabled changes nothing."""
    host.installed("botkit-healthcheck.service").write_text(host.health.read_text())

    result = host.run()

    assert result.returncode == 0
    assert "absent" in result.stdout
    assert not host.installed("botkit-healthcheck.timer").exists()


def test_the_manifest_names_units_canon_really_ships() -> None:
    """The list is only a contract if every line resolves to a file in this repository."""
    names = [
        line.strip()
        for line in MANIFEST.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert names, "the manifest is empty"
    missing = [n for n in names if not list(REPO.glob(f"ops/*/systemd/{n}"))]
    assert not missing, f"manifest names units canon does not ship: {missing}"


def test_the_manifest_excludes_units_this_host_cannot_run() -> None:
    """botkit-e2e describes an unprivileged runner that is not provisioned here.

    Installing it would replace the e2e that runs today with one that has no user, no
    /opt/botkit-e2e and no /etc/botkit-e2e/e2e.env.
    """
    names = MANIFEST.read_text()
    assert "botkit-e2e.service" not in names
    assert "botkit-backlog-stack.service" not in names


def test_sync_canon_installs_units_outside_check_only() -> None:
    source = (REPO / "ops" / "lib" / "sync-canon.sh").read_text()
    step = source.index("=== 7. installed systemd units")
    guard = source.index("CHECK_ONLY", step)
    call = source.index("install-units.sh", step)
    assert guard < call, "--check-only must not touch /etc/systemd/system"
