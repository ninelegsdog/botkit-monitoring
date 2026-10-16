"""Contract tests for ops/e2e/install-webhook-check.sh, run against the shipped script.

Why this file exists: the webhook-check unit runs `User=root`, so its ExecStart cannot
live in the deploy-writable clone — anyone who can write the script would get root at the
next timer tick. It runs from a root-owned copy instead, and until 16.10.2026 *nothing*
refreshed that copy: sync-canon.sh synced the clone and asserted markers in the *clone*,
which is the one thing that updates itself. The production webhook_check.py had drifted
two versions behind canon and no gate said a word; the drift was found by hand.

The installer closes that hole, and these tests pin the joints around it:

  * the file list the installer ships and the path the unit executes are the same joint —
    moving one without the other fails here;
  * sync-canon.sh actually calls the installer (a wiring that can be deleted silently is
    the same defect again);
  * the installer's own failure modes — drift, a missing bundle, an incomplete canon — are
    shown to fail rather than assumed to.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
INSTALLER = REPO / "ops" / "e2e" / "install-webhook-check.sh"
UNIT = REPO / "ops" / "e2e" / "systemd" / "botkit-webhook-check.service"
SYNC_CANON = REPO / "ops" / "lib" / "sync-canon.sh"
DRIFT = REPO / "ops" / "drift" / "check_drift.sh"

SCRIPTS = ("webhook_check.sh", "webhook_check.py", "webhook_check_lib.py", "webhook_metrics.py")
CONF = "fleet.env"
BUNDLE = (*SCRIPTS, CONF)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _install(dest: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return _run("--repo", str(REPO), "--dest", str(dest))


def _mode(path: pathlib.Path) -> int:
    return os.stat(path).st_mode & 0o777


def _code(path: pathlib.Path) -> str:
    """The shell with comment-only lines removed.

    A filename named in prose is not a call. The first version of these assertions passed
    against a check_drift.sh whose *comment* named the installer while the invocation had
    been replaced — the sed that broke it left the wording intact. Found by mutating the
    script, not by reading the test. Assertions here read code, not documentation.
    """
    kept = [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(kept)


def _execstart(unit_text: str) -> str | None:
    match = re.search(r"^ExecStart=(.+)$", unit_text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _installer_default_dest() -> str:
    text = INSTALLER.read_text()
    match = re.search(r"WEBHOOK_CHECK_INSTALL_DIR:-([^}]+)\}", text)
    assert match, "installer no longer names a default destination"
    return match.group(1)


# --- what gets installed ---------------------------------------------------------------------------


def test_the_installer_installs_everything_the_unit_can_run(tmp_path: pathlib.Path) -> None:
    result = _install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(BUNDLE)


def test_the_installed_scripts_are_root_only(tmp_path: pathlib.Path) -> None:
    assert _install(tmp_path).returncode == 0
    for name in SCRIPTS:
        assert _mode(tmp_path / name) == 0o700, f"{name} must not be readable to others"
    assert _mode(tmp_path / CONF) == 0o600, "fleet.env must not be readable to others"


def test_the_installed_files_are_byte_identical_to_the_canon(tmp_path: pathlib.Path) -> None:
    assert _install(tmp_path).returncode == 0
    for name in SCRIPTS:
        assert (tmp_path / name).read_bytes() == (REPO / "ops" / "e2e" / name).read_bytes()
    assert (tmp_path / CONF).read_bytes() == (REPO / "ops" / "lib" / CONF).read_bytes()


def test_reinstalling_an_unchanged_bundle_writes_nothing(tmp_path: pathlib.Path) -> None:
    assert _install(tmp_path).returncode == 0
    second = _install(tmp_path)
    assert second.returncode == 0, second.stderr
    rewritten = [line for line in second.stdout.splitlines() if line.strip().startswith("installed")]
    assert rewritten == [], f"an idempotent run must not rewrite the bundle, got {rewritten}"


# --- the check mode the drift check rides on -------------------------------------------------------


def test_check_passes_on_a_bundle_that_matches_canon(tmp_path: pathlib.Path) -> None:
    assert _install(tmp_path).returncode == 0
    result = _run("--check", "--repo", str(REPO), "--dest", str(tmp_path))
    assert result.returncode == 0, result.stderr


def test_check_reports_a_tampered_bundle_and_fails(tmp_path: pathlib.Path) -> None:
    assert _install(tmp_path).returncode == 0
    with open(tmp_path / "webhook_check.py", "a") as handle:
        handle.write("\n# hand-edit that the canon does not have\n")
    result = _run("--check", "--repo", str(REPO), "--dest", str(tmp_path))
    assert result.returncode != 0, "a bundle that differs from canon must not check clean"
    assert "DRIFT" in result.stdout and "webhook_check.py" in result.stdout


def test_check_fails_when_nothing_is_installed(tmp_path: pathlib.Path) -> None:
    result = _run("--check", "--repo", str(REPO), "--dest", str(tmp_path / "absent"))
    assert result.returncode != 0, "an absent bundle is not the same as a matching one"
    assert "MISSING" in result.stdout


def test_the_installer_refuses_an_incomplete_canon(tmp_path: pathlib.Path) -> None:
    canon = tmp_path / "canon"
    shutil.copytree(REPO / "ops", canon / "ops")
    (canon / "ops" / "e2e" / "webhook_check.py").unlink()
    dest = tmp_path / "dest"
    result = _run("--repo", str(canon), "--dest", str(dest))
    assert result.returncode != 0, "half a bundle must not install"
    assert "FATAL" in result.stderr
    installed = sorted(p.name for p in dest.iterdir()) if dest.exists() else []
    assert installed == [], f"nothing may be installed from a bad canon, got {installed}"


# --- the joints: unit ExecStart, sync-canon wiring, drift detector ---------------------------------


def test_the_unit_runs_a_file_this_installer_installs(tmp_path: pathlib.Path) -> None:
    execstart = _execstart(UNIT.read_text())
    assert execstart, "the unit no longer names an ExecStart"
    target = pathlib.Path(execstart)
    assert target.name in SCRIPTS, "the unit executes a file the installer does not ship"
    assert str(target.parent) == _installer_default_dest(), (
        "the unit's ExecStart directory and the installer's default destination have drifted apart"
    )


def test_the_execstart_reader_can_fail() -> None:
    # The reader above is a regex over the unit text. Prove it reports absence rather than a
    # hardcoded path: a unit without ExecStart must yield None, not a remembered value.
    assert _execstart("[Service]\nExecReload=/bin/true\n") is None


def test_sync_canon_installs_the_bundle() -> None:
    code = _code(SYNC_CANON)
    call = code.find('install-webhook-check.sh" --repo')
    assert call != -1, "sync-canon no longer installs the runtime bundle"
    guard = code.find('CHECK_ONLY" != 1')
    assert guard != -1, "the install step lost its guard against --check-only"
    assert guard < call, "the install step must sit inside the real sync path, not the check-only one"


def test_the_drift_check_compares_the_bundle() -> None:
    code = _code(DRIFT)
    assert 'install-webhook-check.sh" --check' in code, (
        "the drift check no longer compares the runtime bundle with canon"
    )
    assert '--dest "$WEBHOOK_CHECK_DIR"' in code, (
        "the drift check compares something other than the directory it guards"
    )
    assert "WEBHOOK_CHECK_DIR:-/root/botkit-webhook-check" in code, (
        "the drift check reads a directory other than the one sync-canon installs into"
    )


def test_the_comment_stripper_can_fail() -> None:
    # The two tests above read code, not prose. Prove the stripper does not let a comment
    # carry the assertion: with the call commented out, nothing should remain to match.
    text = "# install-webhook-check.sh --check\n: noop\n"
    kept = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "install-webhook-check.sh" not in kept
