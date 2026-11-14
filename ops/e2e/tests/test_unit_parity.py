"""Contract: installed systemd units match the canon.

On 14.11 the installed botkit-rollout-check.service still carried the ReadWritePaths
list from before the textfile metric existed: the poller checked all nine bots and
then died on `mktemp: Read-only file system`. Nothing compared /etc/systemd/system
with canon.

These are contract tests: they verify that the mechanisms for unit parity exist
and are correct, not that the current host is in sync (that is ops/lib/install-units.sh
--check, which runs on the host).
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
E2E_DIR = REPO / "ops" / "e2e"
INSTALL_UNITS = REPO / "ops" / "lib" / "install-units.sh"
MANAGED_UNITS = REPO / "ops" / "lib" / "managed-units.txt"


def test_install_units_script_exists():
    """The install mechanism must exist."""
    assert INSTALL_UNITS.is_file(), "ops/lib/install-units.sh is missing"


def test_install_units_has_check_mode():
    """--check must report drift without touching the host."""
    source = INSTALL_UNITS.read_text()
    assert "--check" in source, "no --check mode"
    assert "CHECK=1" in source or "CHECK_ONLY" in source, "check mode not implemented"


def test_managed_units_manifest_exists_and_nonempty():
    """The manifest must list at least one unit."""
    assert MANAGED_UNITS.is_file(), "ops/lib/managed-units.txt is missing"
    names = [
        line.strip()
        for line in MANAGED_UNITS.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(names) > 0, "manifest is empty"


def test_managed_units_resolve_to_canon_files():
    """Every manifest entry must resolve to a file canon ships."""
    names = [
        line.strip()
        for line in MANAGED_UNITS.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    missing = []
    for name in names:
        hits = list(REPO.glob(f"ops/*/systemd/{name}"))
        if not hits:
            missing.append(name)
    assert not missing, f"manifest names units canon does not ship: {missing}"


def test_managed_units_excludes_e2e_and_backlog():
    """Units for hosts/runners that do not exist here must not be managed."""
    text = MANAGED_UNITS.read_text()
    assert "botkit-e2e.service" not in text, "e2e unit would break the runner"
    assert "botkit-backlog" not in text, "backlog units belong to another host"


def test_install_units_verifies_before_install():
    """systemd-analyze verify must run before installing."""
    source = INSTALL_UNITS.read_text()
    assert "systemd-analyze verify" in source, "no verification before install"


def test_install_units_backs_up_before_overwrite():
    """A replaced unit must be backed up."""
    source = INSTALL_UNITS.read_text()
    assert ".bak-" in source, "no backup before overwrite"


def test_install_units_reloads_daemon():
    """daemon-reload must happen after install."""
    source = INSTALL_UNITS.read_text()
    assert "daemon-reload" in source, "no daemon-reload after install"


def test_install_units_recompares_after():
    """Post-condition: files on disk must equal canon after install."""
    source = INSTALL_UNITS.read_text()
    assert "still differs after install" in source or "re-compares" in source.lower() or \
           "cmp -s" in source, "no post-install comparison"
