"""Contract tests for ops/drift/check_drift.sh, run against the shipped script.

Both defects below were found by reading a log line, not by a failing test.

1. The image-vs-HEAD comparison never ran. `imgsha=$(echo "$img" | sed -n "s#.*/$CHANNEL-
   \\([0-9a-f]\\{7\\}\\).*#\\1#p")` looks for a slash in front of the tag, but the tag follows
   a colon: ghcr.io/ninelegsdog/botkit-bookingbot:v0.8.2-4009418. The substitution returned
   nothing, the `if` body was skipped, and the bot was reported OK while the same log line
   printed an image sha that did not match HEAD. A check that cannot fire is
   indistinguishable from a check that has nothing to report.

2. `send_alert` keyed its throttle on the bot name alone, so a still-open warning swallowed
   the critical alert raised by the same bot for the next six hours: a stopped container or
   a /health of 000 produced "ALERT throttled" and silence.

The tests do not re-implement the logic. Each one builds a sandbox - the real script with
only DEPLOY_ROOT, VALIDATOR and ENV_ROOT repointed, real git clones, stub `docker` and stub
`curl` on PATH - and asserts on what the script logged and posted. Reverting either fix in
the shell script fails here.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time

import pytest

# ops/drift/tests/test_drift_contract.py -> parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]
DRIFT_SH = REPO / "ops" / "drift" / "check_drift.sh"
FLEET_SH = REPO / "ops" / "lib" / "fleet.sh"
FLEET_ENV = REPO / "ops" / "lib" / "fleet.env"

CHANNEL = "v0.8.2"
BOT = "bookingbot"
IMAGE_PREFIX = f"ghcr.io/ninelegsdog/botkit-{BOT}:{CHANNEL}-"

# Runtime paths that decide whether a running image is actually out of date. Documentation,
# tests and licence files are excluded on purpose: deploy.yml skips them, so no image is
# built for those commits and the tag legitimately trails HEAD.
RUNTIME_PATH = "src/handlers.py"
DOCS_PATH = "README.md"
WORKFLOW_PATH = ".github/workflows/deploy.yml"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "contract",
    "GIT_AUTHOR_EMAIL": "contract@example.invalid",
    "GIT_COMMITTER_NAME": "contract",
    "GIT_COMMITTER_EMAIL": "contract@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

DOCKER_STUB = """#!/usr/bin/env bash
# Stand-in for the `docker inspect` calls in check_drift.sh. The script mixes both argument
# orders - `inspect <ctr> -f <fmt>` for the network list and `inspect -f <fmt> <ctr>` for the
# state and the image - so the stub reads the format and the container by shape, not position.
fmt=""; ctr=""
for arg in "$@"; do
  case "$arg" in
    '{{'*) fmt="$arg" ;;
    -*) ;;
    inspect) ;;
    *) [ -z "$ctr" ] && ctr="$arg" ;;
  esac
done
case "$fmt" in
  *NetworkSettings.Networks*) printf '%s\\n' "${STUB_NETWORKS:-{\\"botkit_${ctr#botkit-}\\":{}}}" ;;
  *State.Running*)           printf '%s\\n' "${STUB_RUNNING:-true}" ;;
  *Config.Image*)            printf '%s\\n' "${STUB_IMAGE:-unknown}" ;;
  *)                         printf '\\n' ;;
esac
exit 0
"""

CURL_STUB = """#!/usr/bin/env bash
# Prints an HTTP status where the script expects %{http_code}, and records every call so a
# test can tell an alert that was posted from one that was swallowed by the throttle.
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

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.deploy_root = root / "deploy"
        self.log = root / "drift.log"
        self.curl_log = root / "curl.log"
        self.script = root / "ops" / "drift" / "check_drift.sh"
        self.alerted = root / "alerted"

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        stub_bin = self.root / "bin"
        stub_bin.mkdir(exist_ok=True)
        environ = {
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "STUB_CURL_LOG": str(self.curl_log),
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

    def reset_alert_log(self) -> None:
        self.curl_log.unlink(missing_ok=True)


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> Sandbox:
    """Build the sandbox: the real script, one real clone, one bot in fleet.env."""
    box = Sandbox(tmp_path)

    # The shipped script, with only the three host-specific paths repointed. Everything the
    # tests exercise - the sed, the throttle, the branch logic - stays byte for byte.
    source = DRIFT_SH.read_text()
    rewrites = {
        "DEPLOY_ROOT=": f'DEPLOY_ROOT="{box.deploy_root}"',
        "VALIDATOR=": f'VALIDATOR="{box.root / "noop-validator.sh"}"',
        "ENV_ROOT=": f'ENV_ROOT="{box.root / "envroot"}"',
        "STATE_DIR=": f'STATE_DIR="{box.root / "state"}"',
        "LOG=": f'LOG="{box.log}"',
        # The tracking branch only warns after GRACE_S, and the first observation never
        # alerts at all. Tests that need a warning actually sent shorten the window and
        # run twice; production keeps its half hour.
        "GRACE_S=": "GRACE_S=0",
    }
    for prefix, replacement in rewrites.items():
        assert prefix in source, f"check_drift.sh no longer defines {prefix}"
        source = re.sub(rf"(?m)^{prefix}.*$", replacement.replace("\\", "\\\\"), source)
    box.script.parent.mkdir(parents=True)
    box.script.write_text(source)
    box.script.chmod(0o755)
    (box.root / "noop-validator.sh").write_text("import sys\nsys.exit(0)\n")

    # fleet.sh resolves relative to itself, so lib/ sits next to ops/drift/ as in the repo.
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


def _seed_clone(box: Sandbox, *, second_commit: dict[str, str]) -> str:
    """Create a clone whose HEAD is on origin/main, and return the sha of its first commit.

    The first commit is the one the image was built from; the second is what HEAD points at.
    """
    work = box.root / "seed"
    origin = box.root / "origin.git"
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("init", "--bare", "--initial-branch=main", str(origin), cwd=box.root)
    image_sha = _commit(work, {"bot.py": "print('v1')\n"}, "runtime: first build")
    _commit(work, second_commit, "second commit")
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "origin", "main", cwd=work)

    clone = box.deploy_root / f"botkit-{BOT}"
    clone.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", str(origin), str(clone), cwd=box.root)
    (clone / "deploy").mkdir()
    (clone / "deploy" / "compose.yml").write_text("services: {}\n")
    return image_sha


def test_image_sha_is_read_out_of_a_real_tag(sandbox: Sandbox) -> None:
    """The dead-check defect, end to end: a readable sha produces no IMG OK line."""
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    assert "IMG OK" in sandbox.log_text(), (
        "the script never reached the image comparison - it cannot read the sha from the tag"
    )


def test_docs_only_commit_is_not_reported_as_drift(sandbox: Sandbox) -> None:
    """deploy.yml skips docs, so no image is built and the trailing tag is not drift."""
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "IMG OK" in log
    assert "unbuilt:" not in log, f"a documentation-only commit was reported as unbuilt: {log}"


def test_workflow_only_change_is_not_reported_as_unbuilt(sandbox: Sandbox) -> None:
    """A commit that only changes the CI workflow does not stale the running image.

    Found by running the check on the live fleet on 03.10: the only commits between the
    running tag and HEAD were the two that edited deploy.yml, and with that path in the
    list all nine bots reported as unbuilt. No rollout could clear that except rebuilding
    the whole fleet for a change that never reaches the image.
    """
    image_sha = _seed_clone(sandbox, second_commit={WORKFLOW_PATH: "name: on push\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "IMG OK" in log, log
    assert "unbuilt:" not in log, f"a CI-only commit was reported as unbuilt: {log}"


def test_runtime_commit_is_reported_as_unbuilt(sandbox: Sandbox) -> None:
    """A change that lands inside the image must still raise tracking drift."""
    image_sha = _seed_clone(sandbox, second_commit={RUNTIME_PATH: "changed\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "unbuilt:" in log, f"a runtime change was not reported: {log}"
    assert RUNTIME_PATH in log


def test_sha_missing_from_the_clone_is_reported(sandbox: Sandbox) -> None:
    """An unverifiable sha must not read as "nothing to report"."""
    _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}deadbee")

    assert "not in this clone" in sandbox.log_text(), sandbox.log_text()


def test_unreadable_sha_is_reported(sandbox: Sandbox) -> None:
    """A tag without a channel sha must be visible, not silently skipped."""
    _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"ghcr.io/ninelegsdog/botkit-{BOT}:v0.8.2-nonsha")

    log = sandbox.log_text()
    assert "cannot read the build sha" in log, log


def test_image_off_the_pinned_channel_is_reported(sandbox: Sandbox) -> None:
    _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"ghcr.io/ninelegsdog/botkit-{BOT}:v0.7.0-abcdef0")

    assert "image tag not on channel" in sandbox.log_text(), sandbox.log_text()


def test_healthy_fleet_reports_no_critical(sandbox: Sandbox) -> None:
    """A healthy fleet must be silent, and the health URL must be well formed.

    BASE_URL carries no trailing colon in fleet.env, so the port has to be joined with one.
    Joined without it the URL becomes http://127.0.0.18081/health, curl answers 000, and
    nine healthy bots report nine criticals. The host copy of this script hardcoded the
    address, so the defect only existed in the version that was about to be deployed.
    """
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "CRITICAL" not in log, f"a healthy fleet reported a critical: {log}"
    assert "127.0.0.1:8081/health" in sandbox.alerts_posted(), (
        f"the health URL is malformed: {sandbox.alerts_posted()}"
    )


def test_unhealthy_fleet_is_reported(sandbox: Sandbox) -> None:
    """The counterpart: a failing /health must not stay silent."""
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}", STUB_HEALTH="000")

    assert "/health=000" in sandbox.log_text(), sandbox.log_text()


def test_unreachable_port_reports_one_code_not_two(sandbox: Sandbox) -> None:
    """curl prints its own 000 on a connection failure.

    With "|| echo 000" appended the fallback arrived on top of curl's output and the alert
    read "/health=000000", which looks like a status nobody sent.
    """
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}", STUB_HEALTH="000")

    log = sandbox.log_text()
    assert "/health=000000" not in log, f"the health code was doubled: {log}"
    assert "/health=000 " in log or log.rstrip().endswith("/health=000") or "/health=000)" in log, log


def _arm_warning(sandbox: Sandbox, image: str) -> None:
    """Run until a warning alert is actually posted, so its throttle key exists.

    The first observation only starts the grace window. Backdating the watch marker is the
    same thing the passage of time does, without making the test sleep.
    """
    sandbox.run(STUB_IMAGE=image, STUB_CALL_LABEL="arm1")
    watch = sandbox.root / "state" / "watch" / BOT
    assert watch.exists(), "the tracking branch never started its watch window"
    watch.write_text(str(int(time.time()) - 3600))
    sandbox.reset_alert_log()

    result = sandbox.run(STUB_IMAGE=image, STUB_CALL_LABEL="arm2")
    assert '"severity":"warning"' in sandbox.alerts_posted(), (
        f"no warning alert was posted, so there is no throttle to test against: {result.stderr}"
    )


def test_warning_does_not_swallow_the_critical_alert(sandbox: Sandbox) -> None:
    """The masking defect, end to end: an open warning throttle, then /health fails."""
    image_sha = _seed_clone(sandbox, second_commit={RUNTIME_PATH: "changed\n"})
    image = f"{IMAGE_PREFIX}{image_sha[:7]}"

    _arm_warning(sandbox, image)
    sandbox.reset_alert_log()

    sandbox.run(STUB_IMAGE=image, STUB_HEALTH="000", STUB_CALL_LABEL="health-fail")

    log = sandbox.log_text()
    posted = sandbox.alerts_posted()
    assert "/health=000" in log, f"a /health of 000 was not treated as critical: {log}"
    assert '"severity":"critical"' in posted, f"the critical alert was never posted: {posted}"


def test_repeated_warning_is_throttled(sandbox: Sandbox) -> None:
    """The throttle must keep working, or the fix turns into alert spam."""
    image_sha = _seed_clone(sandbox, second_commit={RUNTIME_PATH: "changed\n"})
    image = f"{IMAGE_PREFIX}{image_sha[:7]}"

    _arm_warning(sandbox, image)
    sandbox.reset_alert_log()

    sandbox.run(STUB_IMAGE=image, STUB_CALL_LABEL="repeat")

    assert "ALERT throttled" in sandbox.log_text(), sandbox.log_text()
    assert '"severity":"warning"' not in sandbox.alerts_posted(), "the repeated warning was posted again"


def test_throttle_keys_are_separate_per_severity(sandbox: Sandbox) -> None:
    """One key per bot is the root cause of the masking, so assert the keys differ."""
    image_sha = _seed_clone(sandbox, second_commit={RUNTIME_PATH: "changed\n"})
    image = f"{IMAGE_PREFIX}{image_sha[:7]}"

    _arm_warning(sandbox, image)
    sandbox.run(STUB_IMAGE=image, STUB_HEALTH="000", STUB_CALL_LABEL="health-fail")

    keys = sorted(p.name for p in (sandbox.root / "state" / "alerted").iterdir())
    assert keys == [f"{BOT}.critical", f"{BOT}.warning"], keys
