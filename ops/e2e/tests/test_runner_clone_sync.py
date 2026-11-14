"""The E2E runner reads its scenarios and its own code out of /opt/botkit-e2e, which is a
git clone that host-setup.sh creates once and never touches again. On 14.11 that clone was
24 commits behind origin/main: every E2E fix merged since 15.10 (including the two fixes
for the chronic BotNotAnswering/UnexpectedReply alerts) could not reach the runner, so
Telegram was paged every six hours for a defect that no longer existed in the repository.

These tests pin the thing that closes that gap: a script that brings the clone to
origin/main before the runner reads it, and the wiring that makes the runner wait for it.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
E2E_DIR = REPO / "ops" / "e2e"
SCRIPT = E2E_DIR / "scripts" / "sync-runner-clone.sh"
SYNC_UNIT = E2E_DIR / "systemd" / "botkit-e2e-sync.service"
E2E_SERVICE = E2E_DIR / "systemd" / "botkit-e2e.service"
HOST_SETUP = E2E_DIR / "scripts" / "host-setup.sh"


def _git(*args, cwd=None, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )


def _setup_origin_and_clone(tmp_path: pathlib.Path):
    """Return (origin, clone, work) where origin has two commits on main and clone is at
    the first one, so the sync script has something to do."""
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin))
    _git("-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", str(work))
    _git("-C", str(work), "config", "user.email", "t@t.com")
    _git("-C", str(work), "config", "user.name", "t")
    (work / "README.md").write_text("v1\n")
    (work / "ops" / "e2e").mkdir(parents=True)
    (work / "ops" / "e2e" / "requirements.txt").write_text("x==1\n")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-q", "-m", "initial")
    _git("-C", str(work), "branch", "-M", "main")
    _git("-C", str(work), "remote", "add", "origin", str(origin))
    _git("-C", str(work), "push", "-q", "origin", "main")

    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone))

    # second commit, on origin only
    (work / "README.md").write_text("v2\n")
    (work / "ops" / "e2e" / "requirements.txt").write_text("x==2\n")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-q", "-m", "second")
    _git("-C", str(work), "push", "-q", "origin", "main")

    return origin, clone, work


def _run_script(clone, env_extra=None):
    env = {**os.environ, "CLONE": str(clone), "RUNNER_USER": "nobody", "BRANCH": "main", "GIT": "git"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), "sync-runner-clone.sh is missing"
    assert os.access(SCRIPT, os.X_OK), "the script must be executable"


def test_sync_unit_runs_the_script():
    unit = SYNC_UNIT.read_text()
    assert "scripts/sync-runner-clone.sh" in unit, "the unit does not run the sync script"
    assert "Type=oneshot" in unit


def test_e2e_service_requires_sync_before_runner():
    unit = E2E_SERVICE.read_text()
    assert "Requires=botkit-e2e-sync.service" in unit, "the runner can run before the clone is updated"
    # After= must name the sync unit so systemd orders them.
    after = [line for line in unit.splitlines() if line.startswith("After=")]
    assert any("botkit-e2e-sync.service" in line for line in after), f"no ordering: {after}"


def test_host_setup_installs_sync_unit():
    src = HOST_SETUP.read_text()
    assert "botkit-e2e-sync.service" in src, "host-setup does not install the sync unit"


def test_script_restores_ownership_and_privacy():
    src = SCRIPT.read_text()
    assert "chown -R" in src and "chmod -R o-rwx" in src, "clone permissions are not restored"
    assert "chmod 0750" in src


def test_clone_reaches_origin_main():
    td = pathlib.Path(tempfile.mkdtemp())
    try:
        _origin, clone, _work = _setup_origin_and_clone(td)
        before = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        result = _run_script(clone)
        assert result.returncode == 0, result.stdout + result.stderr
        after = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        origin_head = subprocess.run(
            ["git", "-C", str(_origin), "rev-parse", "--short", "main"], capture_output=True, text=True, check=True
        ).stdout.strip()
        assert after == origin_head, f"clone {after} != origin {origin_head}"
        assert before != after
        assert "clone" in result.stdout and "->" in result.stdout
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_already_current_reports_so():
    td = pathlib.Path(tempfile.mkdtemp())
    try:
        _origin, clone, _work = _setup_origin_and_clone(td)
        # bring clone to origin first
        _run_script(clone)
        result = _run_script(clone)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "already at origin/main" in result.stdout
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_local_edits_are_discarded():
    td = pathlib.Path(tempfile.mkdtemp())
    try:
        _origin, clone, _work = _setup_origin_and_clone(td)
        (clone / "README.md").write_text("local edit that must not survive\n")
        result = _run_script(clone)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "discarded 1 local change" in result.stdout
        assert (clone / "README.md").read_text() == "v2\n"
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_untracked_files_survive():
    td = pathlib.Path(tempfile.mkdtemp())
    try:
        _origin, clone, _work = _setup_origin_and_clone(td)
        # simulate the venv: an untracked directory that reset --hard must keep
        (clone / "venv" / "bin").mkdir(parents=True)
        (clone / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (clone / "venv" / "bin" / "python").chmod(0o755)
        result = _run_script(clone)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (clone / "venv" / "bin" / "python").is_file(), "reset deleted the untracked venv"
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_requirements_change_reinstalls():
    td = pathlib.Path(tempfile.mkdtemp())
    try:
        _origin, clone, _work = _setup_origin_and_clone(td)
        # fake venv + pip that logs its invocation
        (clone / "venv" / "bin").mkdir(parents=True)
        pip_log = td / "pip.log"
        (clone / "venv" / "bin" / "pip").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(pip_log) + "\nexit 0\n"
        )
        (clone / "venv" / "bin" / "pip").chmod(0o755)
        result = _run_script(clone)
        assert result.returncode == 0, result.stdout + result.stderr
        assert pip_log.is_file(), "pip was not invoked after requirements.txt changed"
        assert any("requirements.txt" in line for line in pip_log.read_text().splitlines()), pip_log.read_text()
        assert "reinstalling into the venv" in result.stdout
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_missing_clone_fails():
    td = pathlib.Path(tempfile.mkdtemp())
    try:
        missing = td / "no-such-clone"
        result = _run_script(missing)
        assert result.returncode != 0
        assert "not a git clone" in result.stderr
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_fetch_failure_fails():
    td = pathlib.Path(tempfile.mkdtemp())
    try:
        _origin, clone, _work = _setup_origin_and_clone(td)
        # point origin at a path that does not exist
        subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", str(td / "nope.git")], check=True)
        result = _run_script(clone)
        assert result.returncode != 0
        assert "git fetch failed" in result.stderr
    finally:
        shutil.rmtree(td, ignore_errors=True)
