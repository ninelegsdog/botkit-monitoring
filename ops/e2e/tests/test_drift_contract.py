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

# Two recipes that genuinely exist in this fleet. Seven bots copy pyproject.toml and src/;
# botkit-reminder, botkit-membership and botkit-bookingbot copy the whole repository.
COMPOSE_PATH = "deploy/compose.yml"

DOCKERFILE_SELECTIVE = """FROM python:3.12-slim
COPY pyproject.toml .
COPY src/ src/
"""
DEPLOY_YAML = """name: deploy
on:
  push:
    branches: [main]
    paths-ignore:
      - '**.md'
      - 'LICENSE'
      - '.github/**'
"""
DOCKERFILE_WHOLE_REPO = """FROM python:3.12-slim
COPY pyproject.toml .
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY . .
"""


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
fmt=""; ctr=""; sub=""
for arg in "$@"; do
  case "$arg" in
    '{{'*) fmt="$arg" ;;
    inspect|manifest) [ -z "$sub" ] && sub="$arg" ;;
    -*) ;;
    *) [ -z "$ctr" ] && ctr="$arg" ;;
  esac
done
# `docker manifest inspect` is how the pinned-tag check asks the registry whether a tag was ever
# published. It needs its own verb here: falling through to the generic branch would make
# "manifest" the container name and answer 0 for every tag, so a nonexistent tag would look
# published and the check would be a check that cannot fail.
if [ "$sub" = "manifest" ]; then
  exit "${STUB_MANIFEST_OK:-0}"
fi
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

    @property
    def env_root(self) -> pathlib.Path:
        """Where the sandbox points ENV_ROOT - the per-bot env files holding IMAGE_TAG."""
        return self.root / "envroot"

    @staticmethod
    def tag_for(sha: str) -> str:
        """A pin is the whole tag, channel prefix included - v0.8.2-<sha>, not <sha>.

        Writing the bare sha is exactly the plausible mistake this check exists to catch, and the
        check caught me at it: the first version of these fixtures seeded IMAGE_TAG=<sha> and the
        whole fleet went CRITICAL, correctly, because that is not a tag compose can resolve.
        """
        return f"{CHANNEL}-{sha[:7]}"

    def write_pin(self, value: str | None) -> None:
        """Write the bot's env file. None means the file exists with no IMAGE_TAG key at all."""
        self.env_root.mkdir(parents=True, exist_ok=True)
        body = "METRICS_PORT=8081\n" if value is None else f"IMAGE_TAG={value}\n"
        self.env_root.joinpath(f"{BOT}.env").write_text(body)


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
            # 0 = the tag exists in the registry. Defaulted to "published" so the older tests keep
            # passing; the one about a nonexistent tag sets it to 1.
            "STUB_MANIFEST_OK": "0",
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
    # The clone needs a Dockerfile: the drift check now reads the recipe to decide what reaches
    # the image, and a sandbox without one is not the fleet - it is the degenerate case the
    # fallback exists for, where the check has to assume everything is unbuilt. Seeding the same
    # selective recipe the majority of bots use, with the runtime file under src/ where that
    # recipe says it belongs.
    image_sha = _commit(
        work,
        {"RUNTIME_PATH": "print('v1')\n", "Dockerfile": DOCKERFILE_SELECTIVE},
        "runtime: first build",
    )
    _commit(work, second_commit, "second commit")
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "origin", "main", cwd=work)

    clone = box.deploy_root / f"botkit-{BOT}"
    clone.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", str(origin), str(clone), cwd=box.root)
    (clone / "deploy").mkdir()
    (clone / "deploy" / "compose.yml").write_text("services: {}\n")
    # The env file carries IMAGE_TAG, and since 04.10 it is the source of truth for the image, so a
    # healthy sandbox has to have one. Without this every existing test went CRITICAL on "IMAGE_TAG
    # not set" - which is the check working, and the sandbox lying about what a healthy fleet is.
    box.write_pin(box.tag_for(image_sha))
    return image_sha


def _seed_with_recipe(box: Sandbox, dockerfile: str, second_commit: dict[str, str]) -> str:
    """Clone whose first commit is the image build, with a Dockerfile and a tracked compose file.

    Two things this has to get right, both of which make a test pass for the wrong reason if it
    does not. The compose file must be tracked and present at the image sha, or `git diff` between
    the running tag and HEAD can never mention it. And each recipe needs its own seed and origin:
    reusing one directory makes the second call commit on top of the first, so the sha it returns
    is the second commit and the "first build" no longer describes the same tree the recipe is in.
    """
    tag = f"{abs(hash(dockerfile)) % 100000:05d}"
    work = box.root / f"seed_{tag}"
    origin = box.root / f"origin_{tag}.git"
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("init", "--bare", "--initial-branch=main", str(origin), cwd=box.root)
    image_sha = _commit(
        work,
        {
            RUNTIME_PATH: "print('v1')\n",
            "Dockerfile": dockerfile,
            COMPOSE_PATH: "services: {}\n",
            # как на проде: deploy.yml с paths-ignore лежит в репо с первой сборки
            ".github/workflows/deploy.yml": DEPLOY_YAML,
        },
        "runtime: first build",
    )
    _commit(work, second_commit, "second commit")
    _git("remote", "add", "origin", str(origin), cwd=work)
    _git("push", "origin", "main", cwd=work)

    clone = box.deploy_root / f"botkit-{BOT}"
    clone.parent.mkdir(parents=True, exist_ok=True)
    if clone.exists():
        # Fetch by path, not by the name "origin": after the first recipe the clone's origin points
        # at a different bare repo, and `reset --hard origin/main` would silently restore the
        # previous recipe instead of this one.
        _git("fetch", str(origin), "main", cwd=clone)
        _git("reset", "--hard", "FETCH_HEAD", cwd=clone)
        _git("clean", "-fd", cwd=clone)
    else:
        _git("clone", str(origin), str(clone), cwd=box.root)
    box.write_pin(box.tag_for(image_sha))
    return image_sha


def test_a_compose_only_change_is_not_drift_when_the_recipe_ignores_deploy(sandbox: Sandbox) -> None:
    """The false positive that put all nine bots in DRIFT on 03.10, for the seven bots where it holds.

    The compose change that fixed the image pinning touched no file that reaches the image in
    these seven bots, so no rebuild could have changed anything - and the drift check reported it
    as an unbuilt runtime change anyway, on every bot, for hours.
    """
    image_sha = _seed_with_recipe(
        sandbox, DOCKERFILE_SELECTIVE, {COMPOSE_PATH: "services: {}\n# pinned\n"}
    )
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "IMG OK" in log, f"a compose-only change was called unbuilt:\n{log}"
    assert "unbuilt:" not in log, f"compose reported as an unbuilt runtime change:\n{log}"


def test_a_compose_change_is_drift_when_the_recipe_copies_the_whole_repo(sandbox: Sandbox) -> None:
    """The other three bots really do ship deploy/ inside the image, so there it is real drift.

    This is why a single hardcoded path list cannot be right: the same commit is drift for
    reminder and a non-event for docuflow.
    """
    image_sha = _seed_with_recipe(
        sandbox, DOCKERFILE_WHOLE_REPO, {COMPOSE_PATH: "services: {}\n# pinned\n"}
    )
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "unbuilt:" in log, (
        f"`COPY . .` means the compose file is in the image, but the check called it clean:\n{log}"
    )
    assert COMPOSE_PATH in log, f"the log does not name the file that is unbuilt:\n{log}"


def test_a_source_change_is_drift_under_either_recipe(sandbox: Sandbox) -> None:
    """Narrowing the list must not blind it. src/ is in the image for every bot."""
    for name, recipe in (("selective", DOCKERFILE_SELECTIVE), ("whole", DOCKERFILE_WHOLE_REPO)):
        sandbox.reset_alert_log()
        image_sha = _seed_with_recipe(sandbox, recipe, {RUNTIME_PATH: "print('v2')\n"})
        sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")
        log = sandbox.log_text()
        assert "unbuilt:" in log, f"{name} recipe: a source change was not reported:\n{log}"
        assert RUNTIME_PATH in log, f"{name} recipe: the log does not name src/:\n{log}"


def test_an_unreadable_recipe_reports_rather_than_clears(sandbox: Sandbox) -> None:
    """Not knowing what reaches the image must not read as "nothing to report".

    This is the branch that made the sandbox itself lie: the seeded clone had no Dockerfile, the
    path list came back empty, and a conservative fallback that reports everything turned every
    documentation-only commit into an unbuilt change. The opposite mistake is worse - clearing
    the check when the recipe cannot be read means a bot with a broken or missing Dockerfile is
    reported as clean, forever, and the difference is invisible.
    """
    # A recipe that copies nothing from the repository: no paths, so the fallback must engage.
    no_repo_copy = """FROM python:3.12-slim
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
"""
    image_sha = _seed_with_recipe(sandbox, no_repo_copy, {DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")
    log = sandbox.log_text()
    assert "unbuilt:" in log, (
        f"a recipe with no repository COPY left the path list empty and the change was called "
        f"clean:\n{log}"
    )

    # And the same when the file is not there at all.
    sandbox2_image = _seed_with_recipe(sandbox, DOCKERFILE_SELECTIVE, {DOCS_PATH: "docs\n"})
    (sandbox.deploy_root / f"botkit-{BOT}" / "Dockerfile").unlink()
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{sandbox2_image[:7]}")
    log = sandbox.log_text()
    assert "unbuilt:" in log, f"a missing Dockerfile was treated as nothing to report:\n{log}"


def test_the_container_must_match_the_pinned_tag(sandbox: Sandbox) -> None:
    """The env file is the source of truth, so the container has to agree with it.

    IMAGE_TAG has been the single source of truth for the image since 04.10. Nothing reconciled it
    against the running container, and I checked that pairing by hand after every command this
    session - nine times over - because there was no check for it. A hand-edited IMAGE_TAG is the
    case that matters: compose would keep resolving the new value while the container ran the old
    image, and the two would disagree silently until the next restart. That happened live on
    03.10, when a rollout to :main left the file pinned to v0.8.2-84010fa while the container ran
    :main, and nothing noticed.
    """
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.write_pin(sandbox.tag_for(image_sha))
    clean = sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")
    assert clean.returncode == 0, (
        f"a matching pin was not accepted, so the test below proves nothing:\n{sandbox.log_text()}"
    )

    # Same image, different pin: the file and the container now disagree.
    sandbox.write_pin("v0.8.2-0000000")
    result = sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert result.returncode == 1, f"a container running a different tag than the pin was accepted:\n{log}"
    assert "does not match IMAGE_TAG" in log, f"the mismatch never reached the log:\n{log}"


def test_a_missing_pin_is_critical(sandbox: Sandbox) -> None:
    """No IMAGE_TAG means compose refuses to start the bot at all - a CRITICAL, not a warning."""
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.write_pin(None)
    result = sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert result.returncode == 1, f"a bot with no IMAGE_TAG was reported healthy:\n{log}"
    assert "IMAGE_TAG not set" in log, f"the missing pin never reached the log:\n{log}"


def test_a_pin_that_was_never_published_is_reported(sandbox: Sandbox) -> None:
    """Compose resolves any tag it is given; only the registry knows whether it exists.

    The compose validator checks the form of the reference, so a typo in IMAGE_TAG passes
    validation, compose starts, and the failure lands on the next `docker pull` instead.
    """
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.write_pin(sandbox.tag_for(image_sha))
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}", STUB_MANIFEST_OK="1")

    log = sandbox.log_text()
    assert "not published in" in log, (
        f"a tag that does not exist in the registry was not reported:\n{log}"
    )


def test_a_digest_reference_is_not_compared_as_if_it_had_a_tag(sandbox: Sandbox) -> None:
    """`${img##*:}` on a digest reference yields a fragment of the hash, not a tag.

    A digest-pinned deployment would then be reported as disagreeing with a perfectly correct pin,
    every run, for ever - the same shape of defect as the other hardcoded substitutions in this
    file: a comparison that fires on everything is not a comparison.
    """
    image_sha = _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    sandbox.write_pin(sandbox.tag_for(image_sha))
    digest = f"ghcr.io/ninelegsdog/botkit-{BOT}@sha256:{'a1b2c3d4' * 8}"
    sandbox.run(STUB_IMAGE=digest, STUB_MANIFEST_OK="0")

    log = sandbox.log_text()
    assert "does not match IMAGE_TAG" not in log, (
        f"a digest reference was compared as a tag:\n{log}"
    )


def test_the_recipe_is_read_rather_than_a_handwritten_list() -> None:
    """The list must come from the Dockerfile. A hand-maintained one is what broke.

    The removed line was `bot.py src deploy pyproject.toml Dockerfile docker-compose.yml`. It
    named `deploy`, which is inside the image for three bots and outside it for seven, so it was
    simultaneously too broad and too narrow - and nobody could tell which bot it was right about.
    """
    source = DRIFT_SH.read_text()
    assert "dockerfile_repo_paths" in source, "the drift check no longer consults the Dockerfile"
    assert "bot.py src deploy pyproject.toml Dockerfile docker-compose.yml" not in source, (
        "the hand-written path list is back"
    )


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


def test_workflow_only_change_is_not_unbuilt_under_whole_repo_recipe(sandbox: Sandbox) -> None:
    """Боты с COPY . . (reminder, membership, bookingbot): CI-only коммит не дрейфует.

    Их Dockerfile копирует весь репозиторий, поэтому .github/ технически попадает в
    слой образа - но deploy.yml его не собирает (paths-ignore), и тег легитимно
    отстаёт. Без этого правила три бота дрейфовали навсегда после волны пиннинга
    workflow 06.10: построить образ из одного .github-коммита нечем и не нужно.
    """
    second = {WORKFLOW_PATH: DEPLOY_YAML + "# pinned by sha\n"}
    image_sha = _seed_with_recipe(sandbox, DOCKERFILE_WHOLE_REPO, second)
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "IMG OK" in log, log
    assert "unbuilt:" not in log, f"a CI-only commit was reported as unbuilt: {log}"


def test_docs_only_commit_is_not_unbuilt_under_whole_repo_recipe(sandbox: Sandbox) -> None:
    """README под **.md deploy.yml тоже не собирает - образ отстаёт законно."""
    image_sha = _seed_with_recipe(sandbox, DOCKERFILE_WHOLE_REPO, {DOCS_PATH: "docs\n"})
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}{image_sha[:7]}")

    log = sandbox.log_text()
    assert "IMG OK" in log, log
    assert "unbuilt:" not in log, f"a docs-only commit was reported as unbuilt: {log}"


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
    # The pin has to be moved with the image. Since 04.10 the env file is the source of truth, and
    # a disagreement between it and the container is CRITICAL and stops the run - so a test that
    # injects a different image without moving the pin would die on that critical before reaching
    # the branch it is here to observe.
    sandbox.write_pin("v0.8.2-deadbee")
    sandbox.run(STUB_IMAGE=f"{IMAGE_PREFIX}deadbee")

    assert "not in this clone" in sandbox.log_text(), sandbox.log_text()


def test_unreadable_sha_is_reported(sandbox: Sandbox) -> None:
    """A tag without a channel sha must be visible, not silently skipped."""
    _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    # The pin has to be moved with the image. Since 04.10 the env file is the source of truth, and
    # a disagreement between it and the container is CRITICAL and stops the run - so a test that
    # injects a different image without moving the pin would die on that critical before reaching
    # the branch it is here to observe.
    sandbox.write_pin("v0.8.2-nonsha")
    sandbox.run(STUB_IMAGE=f"ghcr.io/ninelegsdog/botkit-{BOT}:v0.8.2-nonsha")

    log = sandbox.log_text()
    assert "cannot read the build sha" in log, log


def test_image_off_the_pinned_channel_is_reported(sandbox: Sandbox) -> None:
    _seed_clone(sandbox, second_commit={DOCS_PATH: "docs\n"})
    # The pin has to be moved with the image. Since 04.10 the env file is the source of truth, and
    # a disagreement between it and the container is CRITICAL and stops the run - so a test that
    # injects a different image without moving the pin would die on that critical before reaching
    # the branch it is here to observe.
    sandbox.write_pin("v0.7.0-abcdef0")
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
