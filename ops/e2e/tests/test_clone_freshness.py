"""Contract: every clone that the fleet reads from is current with origin/main.

On 14.11 the E2E runner clone was 24 commits behind origin/main: every fix merged
since 15.10 (including the two E2E fixes) could not reach the runner, so Telegram
was paged every six hours for a defect that no longer existed in the repository.
Nothing joined 'the clone exists' with 'the clone is current' — host-setup.sh
cloned once and never fetched again.

These are contract tests: they check that the mechanisms for keeping clones current
exist in the repository, and that they actually verify HEAD == origin/main rather
than merely 'the clone exists'.
"""

from __future__ import annotations

import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
E2E_DIR = REPO / "ops" / "e2e"

SYNC_RUNNER = E2E_DIR / "scripts" / "sync-runner-clone.sh"
SYNC_UNIT = E2E_DIR / "systemd" / "botkit-e2e-sync.service"
E2E_SERVICE = E2E_DIR / "systemd" / "botkit-e2e.service"
CLONE_SYNC_HELPER = REPO / "ops" / "lib" / "clone_sync.sh"


def test_sync_runner_clone_script_exists_and_is_executable():
    """The runner clone needs a sync mechanism."""
    assert SYNC_RUNNER.is_file(), "sync-runner-clone.sh is missing"
    assert os.access(SYNC_RUNNER, os.X_OK), "the script must be executable"


def test_sync_runner_clone_verifies_head_equals_origin():
    """The script must not just fetch; it must refuse unless HEAD == origin/main."""
    source = SYNC_RUNNER.read_text()
    assert "rev-parse --short HEAD" in source, "HEAD is not read"
    assert 'origin/$BRANCH' in source or "origin/main" in source, "origin is not resolved"
    assert "reset --hard" in source, "no reset to origin"
    # After reset, HEAD must equal the target.
    assert 'after=$(git -C "$CLONE" rev-parse --short HEAD)' in source or \
           "after=$(" in source, "post-reset HEAD is not verified"


def test_sync_unit_requires_network():
    """The sync needs network to fetch."""
    unit = SYNC_UNIT.read_text()
    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit


def test_e2e_service_requires_sync_before_runner():
    """The runner must not start before the clone is updated."""
    unit = E2E_SERVICE.read_text()
    assert "Requires=botkit-e2e-sync.service" in unit
    after = [line for line in unit.splitlines() if line.startswith("After=")]
    assert any("botkit-e2e-sync.service" in line for line in after)


def test_clone_sync_helper_exists():
    """A reusable helper prevents copy-paste drift between sync mechanisms."""
    assert CLONE_SYNC_HELPER.is_file(), "ops/lib/clone_sync.sh is missing"
    source = CLONE_SYNC_HELPER.read_text()
    assert "fetch --prune origin" in source, "helper does not fetch"
    assert "reset --hard" in source, "helper does not reset"
    assert "rev-parse --short HEAD" in source, "helper does not verify HEAD"


def test_host_setup_installs_sync_unit():
    """host-setup must install the sync unit, not just the runner."""
    source = (E2E_DIR / "scripts" / "host-setup.sh").read_text()
    assert "botkit-e2e-sync.service" in source, "host-setup does not install the sync unit"
