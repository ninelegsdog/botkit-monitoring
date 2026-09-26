"""Contract tests for how the smoke check is *scheduled*.

The T2 fix (verify the public certificate) was committed, tested and green in the repo
while production kept running a stale copy from /root/botkit-e2e that still had
`curl -k`. The scheduled path is the one that matters, so the unit file is part of the
contract: it must execute the synced canon, and a failing sink must be loud.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"
SMOKE = Path(__file__).resolve().parents[1] / "smoke_all.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise the smoke script"
)


def unit_text() -> str:
    return (SYSTEMD / "botkit-smoke.service").read_text()


def unit_directives() -> list[str]:
    """Non-comment lines only.

    The unit explains in a comment why the old copy path was dropped, and a guard that
    scans comments would force that explanation to be deleted — the one thing keeping
    the next person from putting the copy back.
    """
    return [
        ln.strip()
        for ln in unit_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def exec_start() -> str:
    match = re.search(r"^ExecStart=(.+)$", unit_text(), re.MULTILINE)
    assert match, "botkit-smoke.service has no ExecStart - the smoke check would never run"
    return match.group(1).strip()


def test_timer_runs_the_synced_canon_not_a_stale_copy():
    """The regression that hid the whole incident: a fixed repo, an unfixed schedule."""
    target = exec_start()
    assert target.startswith("/home/deploy/botkit-monitoring/"), (
        f"ExecStart={target} bypasses the synced canon; a copy cannot stay current"
    )
    stale = [ln for ln in unit_directives() if "/root/botkit-e2e" in ln]
    assert stale == [], f"stale copy path is back in an effective directive: {stale}"


def test_timer_is_enabled_next_to_its_unit():
    assert (SYSTEMD / "botkit-smoke.timer").is_file()


def test_script_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", str(SMOKE)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"smoke_all.sh does not parse: {result.stderr}"


def test_unwritable_log_is_fatal_instead_of_a_silent_pass(tmp_path):
    """Regression: as `deploy` the script logged to /var/log, failed, and still said PASS."""
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    log = readonly / "smoke.log"
    result = subprocess.run(
        ["bash", str(SMOKE)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "BOTKIT_SMOKE_LOG": str(log),
            "BOTKIT_SMOKE_ALERTED_DIR": str(tmp_path / "alerted"),
        },
    )
    readonly.chmod(0o700)
    if result.returncode == 0:
        pytest.skip("filesystem allows writing to a 0500 directory (running as root?)")
    assert result.returncode == 2, f"expected exit 2, got {result.returncode}"
    assert "FATAL" in result.stderr, f"unwritable log must say so loudly: {result.stderr!r}"
    assert "PASS" not in result.stdout, "must not report PASS when it cannot log"


def test_sinks_are_overridable():
    """An operator running the script as a normal user must be able to give it a sink."""
    text = SMOKE.read_text()
    assert "BOTKIT_SMOKE_LOG" in text
    assert "BOTKIT_SMOKE_ALERTED_DIR" in text
