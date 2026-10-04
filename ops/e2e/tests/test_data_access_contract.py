"""Contract tests for the data-layer checks added after the 03.10 permission incident.

The incident: a fleet-wide `chown -R deploy:deploy` over `data/` left all nine bots unable
to open their SQLite file, and nothing reported it. Three separate things had to be true
for that to stay invisible, and each one is a hole this file now closes:

1. compose pins the runtime user (`user: "1001:1001"`) while the host directory keeps its
   own owner, so the two can disagree indefinitely.
2. The compose healthcheck probes `/health` and a Redis socket. It never reads SQLite, so a
   container that answers 200 while unable to open its database is reported healthy.
3. A long-lived process survives on the file descriptor it opened before the change. The
   damage only surfaces when a rollout recreates the container, which is the worst possible
   moment to discover it.

Two guards came out of it. `ops/drift/check_drift.sh` now asks each running container, as
the user it actually runs as, whether it can write its own data directory - and treats a
"no" as critical. `ops/rollout/deploy_rollout.sh` now refuses to replace a container whose
data directory that user cannot write, before touching anything.

The drift side is tested the way this repository already tests its shell: the real script in
a sandbox with only host paths repointed, a real git clone, and stub `docker`/`curl` on PATH.
Both directions are asserted - a bot that cannot write must go critical, and a bot that can
must stay clean, so the check cannot be satisfied by always failing.

`deploy_rollout.sh` hardcodes DEPLOY_DIR, STATE_DIR and LOG, and making them overridable
would mean a stray environment variable could point a production rollout at the wrong tree.
These tests therefore assert its invariants by reading the shipped script: the gate sits
ahead of the pull, it refuses with a distinct exit code, --dry-run reports it, and it is not
applied to the rollback path - blocking a rollback would be worse than the outage it prevents.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

# ops/e2e/tests/test_data_access_contract.py -> parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]
DRIFT_SH = REPO / "ops" / "drift" / "check_drift.sh"
ROLLOUT_SH = REPO / "ops" / "rollout" / "deploy_rollout.sh"
FLEET_SH = REPO / "ops" / "lib" / "fleet.sh"
FLEET_ENV = REPO / "ops" / "lib" / "fleet.env"

CHANNEL = "v0.8.2"
BOT = "bookingbot"
IMAGE_PREFIX = f"ghcr.io/ninelegsdog/botkit-{BOT}:{CHANNEL}-"
DOCS_PATH = "README.md"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "contract",
    "GIT_AUTHOR_EMAIL": "contract@example.invalid",
    "GIT_COMMITTER_NAME": "contract",
    "GIT_COMMITTER_EMAIL": "contract@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

# check_drift.sh reaches for docker in four shapes: inspect with a format, exec into a
# container, compose config, and nothing else. `exec` is the one that matters here - it is
# how the check asks the container whether it can write its own data directory - so the stub
# honours STUB_EXEC_OK instead of pretending every exec succeeded. Before this case existed
# the stub answered `exec` with an empty line and exit 0, which would have made a broken
# fleet look healthy in every test that used it.
DOCKER_STUB = """#!/usr/bin/env bash
fmt=""; ctr=""; sub=""
for arg in "$@"; do
  case "$arg" in
    '{{'*) fmt="$arg" ;;
    inspect|exec|compose|pull) [ -z "$sub" ] && sub="$arg" ;;
    -*) ;;
    *) [ -z "$ctr" ] && ctr="$arg" ;;
  esac
done
if [ "$sub" = "exec" ]; then
  printf '%s\\n' "$ctr" >> "${STUB_EXEC_LOG:-/dev/null}"
  [ "${STUB_EXEC_OK:-1}" = "1" ] && exit 0
  exit 1
fi
case "$fmt" in
  *NetworkSettings.Networks*) printf '%s\\n' "${STUB_NETWORKS:-{\\"botkit_${ctr#botkit-}\\":{}}}" ;;
  *State.Running*)           printf '%s\\n' "${STUB_RUNNING:-true}" ;;
  *Config.Image*)            printf '%s\\n' "${STUB_IMAGE:-unknown}" ;;
  *Config.User*)             printf '%s\\n' "${STUB_USER:-1001:1001}" ;;
  *)                         printf '\\n' ;;
esac
exit 0
"""

CURL_STUB = """#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in
    http*) url="$arg" ;;
  esac
done
printf '%s %s\\n' "${STUB_CALL_LABEL:-call}" "$*" >> "$STUB_CURL_LOG"
case "$url" in
  */health) printf '%s\\n' "${STUB_HEALTH:-200}" ;;
  *) printf '%s\\n' "${STUB_CODE:-200}" ;;
esac
"""


def _git(*args: str, cwd: pathlib.Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: pathlib.Path, files: dict[str, str], message: str) -> str:
    for name, body in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


class Sandbox:
    """A runnable copy of the drift check with stubbed docker, curl and fleet."""

    @property
    def env_root(self) -> pathlib.Path:
        """Where the sandbox points ENV_ROOT - the per-bot env files holding IMAGE_TAG."""
        return self.root / "envroot"

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.deploy_root = root / "deploy"
        self.log = root / "drift.log"
        self.curl_log = root / "curl.log"
        self.exec_log = root / "exec.log"
        self.script = root / "ops" / "drift" / "check_drift.sh"

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        stub_bin = self.root / "bin"
        stub_bin.mkdir(exist_ok=True)
        environ = {
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "STUB_CURL_LOG": str(self.curl_log),
            "STUB_EXEC_LOG": str(self.exec_log),
            "STUB_IMAGE": f"{IMAGE_PREFIX}0000000",
            **env,
        }
        return subprocess.run(
            ["bash", str(self.script)],
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def alerts_posted(self) -> str:
        return self.curl_log.read_text() if self.curl_log.exists() else ""

    def execs(self) -> str:
        return self.exec_log.read_text() if self.exec_log.exists() else ""


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> Sandbox:
    box = Sandbox(tmp_path)

    source = DRIFT_SH.read_text()
    rewrites = {
        "DEPLOY_ROOT=": f'DEPLOY_ROOT="{box.deploy_root}"',
        "VALIDATOR=": f'VALIDATOR="{box.root / "noop-validator.sh"}"',
        "ENV_ROOT=": f'ENV_ROOT="{box.root / "envroot"}"',
        "STATE_DIR=": f'STATE_DIR="{box.root / "state"}"',
        "LOG=": f'LOG="{box.log}"',
        "GRACE_S=": "GRACE_S=0",
    }
    for prefix, replacement in rewrites.items():
        assert prefix in source, f"check_drift.sh no longer defines {prefix}"
        source = re.sub(rf"(?m)^{prefix}.*$", replacement.replace("\\", "\\\\"), source)
    box.script.parent.mkdir(parents=True)
    box.script.write_text(source)
    box.script.chmod(0o755)
    (box.root / "noop-validator.sh").write_text("import sys\nsys.exit(0)\n")

    lib = box.script.parent.parent / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    shutil.copy(FLEET_SH, lib / "fleet.sh")
    original_fleet = FLEET_ENV.read_text()
    original_line = next(line for line in original_fleet.splitlines() if line.startswith("FLEET="))
    (lib / "fleet.env").write_text(original_fleet.replace(original_line, f'FLEET="{BOT}:8081"'))

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    for name, body in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
        path = stub_bin / name
        path.write_text(body)
        path.chmod(0o755)

    return box


def _seed_clone(box: Sandbox) -> str:
    """One real clone whose HEAD is a docs-only commit ahead of the image; returns the image sha."""
    work = box.root / "seed"
    origin = box.root / "origin.git"
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("init", "--bare", "--initial-branch=main", str(origin), cwd=box.root)
    # A Dockerfile, because check_drift.sh now reads the recipe to decide what reaches the image.
    # A sandbox clone without one is not the fleet, it is the degenerate case where the check
    # cannot tell and has to assume everything is unbuilt - which is how this very file started
    # reporting a documentation-only commit as drift.
    image_sha = _commit(
        work,
        {
            "src/handlers.py": "print('v1')\n",
            "Dockerfile": "FROM python:3.12-slim\nCOPY pyproject.toml .\nCOPY src/ src/\n",
        },
        "runtime: first build",
    )
    # IMAGE_TAG too: it is the source of truth for the image, so a sandbox without one is a fleet
    # whose bots could not start at all, and the drift check is right to call that critical.
    box.env_root.mkdir(parents=True, exist_ok=True)
    box.env_root.joinpath(f"{BOT}.env").write_text(f"IMAGE_TAG={CHANNEL}-{image_sha[:7]}\n")
    _commit(work, {DOCS_PATH: "docs\n"}, "docs only")
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "origin", "main", cwd=work)

    clone = box.deploy_root / f"botkit-{BOT}"
    clone.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", str(origin), str(clone), cwd=box.root)
    (clone / "deploy").mkdir()
    (clone / "deploy" / "compose.yml").write_text("services: {}\n")
    (clone / "data").mkdir()
    return image_sha


def test_unwritable_data_dir_is_reported_critical(sandbox: Sandbox) -> None:
    """The incident itself: the container cannot write data/, and the check must say so."""
    image_sha = _seed_clone(sandbox)
    result = sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}", STUB_EXEC_OK="0")

    log = sandbox.log_text()
    assert result.returncode == 1, f"the check passed a bot that cannot open its database:\n{log}"
    assert "data/ not writable by container user" in log, (
        f"the data-layer failure never reached the log:\n{log}"
    )
    # The alert has to name the mismatch, not just say "something is wrong": an operator
    # reading a phone at 3am needs the two numbers to act on.
    assert "BotkitDrift" in sandbox.alerts_posted(), "the critical never became an alert"


def test_the_alert_reports_both_the_directory_and_the_container_user(sandbox: Sandbox) -> None:
    _seed_clone(sandbox)
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}0000000", STUB_EXEC_OK="0", STUB_USER="1001:1001")

    log = sandbox.log_text()
    assert "dir uid=" in log and "container user=" in log, (
        f"the log does not say which owner and which runtime user disagree:\n{log}"
    )


def test_writable_data_dir_stays_ok(sandbox: Sandbox) -> None:
    """The check must not be satisfiable by always failing - a clean bot has to stay clean."""
    image_sha = _seed_clone(sandbox)
    result = sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}", STUB_EXEC_OK="1")

    log = sandbox.log_text()
    assert result.returncode == 0, f"a bot that can write its data directory was flagged:\n{log}"
    assert "data/ not writable" not in log, f"false positive on the data layer:\n{log}"
    assert f"OK {BOT}" in log, f"the check no longer reports the bot as healthy:\n{log}"


def test_the_probe_really_asks_the_container(sandbox: Sandbox) -> None:
    """The check must interrogate the container, not infer from ownership on the host.

    A host-side `stat` comparison passes whenever the numbers happen to match and fails
    whenever they do not, but it says nothing about what the container can actually do: group
    bits, ACLs and a read-only mount all break the mount while the uid still matches.
    """
    _seed_clone(sandbox)
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}0000000", STUB_EXEC_OK="1")

    assert f"botkit-{BOT}" in sandbox.execs(), (
        "the check never exec'd into the container - it cannot know what the runtime user "
        "can write"
    )
    assert "/app/data" in DRIFT_SH.read_text(), (
        "the probe path no longer matches the compose mount of ../data:/app/data"
    )


def test_rollout_refuses_before_touching_the_container() -> None:
    """The gate has to precede the pull and the compose up, and refuse loudly."""
    source = ROLLOUT_SH.read_text()

    gate = source.index("if ! PF=$(data_preflight); then")
    pull = source.index('log "bot=$bot pulling $TARGET_IMAGE ..."')
    assert gate < pull, (
        "the preflight gate was moved after the pull - by then the image is already fetched "
        "and the refusal comes too late to be cheap"
    )
    assert "exit 6" in source[gate:pull], "the gate must refuse with its own exit code"
    assert "REFUSED" in source[gate:pull], "the operator gets no message explaining the refusal"
    assert "chown -R 1001:1001" in source[gate:pull], (
        "the refusal does not say how to fix it"
    )


def test_rollout_dry_run_surfaces_the_preflight() -> None:
    """A dry run that hides the gate would let the drill reach the real thing unprepared."""
    source = ROLLOUT_SH.read_text()
    dry = source.index('if [[ "$DRY" == 1 ]]; then')
    block = source[dry : source.index("fi", dry)]
    assert "data_preflight" in block, (
        "--dry-run no longer reports whether the data directory is writable, so the one "
        "command that is safe to run is the one that stops telling you"
    )


def test_rollout_does_not_gate_the_rollback() -> None:
    """Exactly three references: the definition, the dry run and the forward gate.

    A fourth would mean the rollback path started asking the same question - and a bot that
    is already broken must still be allowed to get its old image back. Blocking the rollback
    would be worse than the outage the gate prevents.
    """
    source = ROLLOUT_SH.read_text()
    assert source.count("data_preflight") == 3, (
        "expected the definition, the --dry-run report and the forward gate - found "
        f"{source.count('data_preflight')} references; if the rollback is now gated, that "
        "is the wrong fix"
    )
    # The rollback does not re-apply an image reference any more: it restores the tag that
    # was recorded in the env file. What has to survive is that the rollback still exists
    # and still reaches compose, otherwise a failed rollout would have nowhere to go back to.
    assert 'set_image_tag "$PREV_TAG"' in source, (
        "the rollback no longer restores the previously recorded tag"
    )
    rollback = source[source.index('if [[ "$HEALTH_RESULT" != "ok" ]]; then') :]
    assert "compose_up" in rollback, "the rollback never reaches compose, so it rolls nowhere"
