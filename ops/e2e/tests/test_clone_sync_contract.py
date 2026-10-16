"""Contract tests for the clone-freshness gate in deploy_rollout.sh, added after the
13.10 drift incident (deferred-work item D12).

The footgun this closes: a rollout updates the *image* (it pins IMAGE_TAG and recreates the
container) but the bot's git clone under /home/deploy is a separate artifact that a rollout
never touched. On 13.10 every container ran the current image while all nine clones sat behind
their main, so the drift check — which compares each clone's HEAD with origin/main — went red
for the fleet. The images were current; the checkouts were not. Nothing joined the two actions:
`sync-canon.sh` syncs only the monitoring clone, and the rollout synced nothing.

The fix is a gate inside the rollout: before it touches the container, bring the clone to
origin/main with `git pull --ff-only` as the clone's owner, and refuse (exit) unless
HEAD == origin/main afterwards. Placed *before* the idempotent "already on target image" skip,
because in exactly the drift case the image already equals the target and the skip would return
early over the stale checkout the gate exists to catch.

Like the image-pin contract, these tests read the shipped script rather than running it: the
rollout hardcodes DEPLOY_DIR and runs as root against production clones, so making its paths
overridable for a test would be the very hazard the gate is meant to remove. They assert the
shape that has to hold — ordering, the user the git runs as, the ff-only discipline, and the
refusal.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
ROLLOUT_SH = REPO / "ops" / "rollout" / "deploy_rollout.sh"
CHECK_UPDATES_SH = REPO / "ops" / "rollout" / "check_updates.sh"
UNIT = REPO / "ops" / "rollout" / "systemd" / "botkit-rollout-check.service"


@pytest.fixture(scope="module")
def rollout() -> str:
    return ROLLOUT_SH.read_text()


def _body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_the_rollout_syncs_the_clone(rollout: str) -> None:
    """A rollout without a clone sync is the 13.10 defect, stated positively."""
    assert "sync_clone() {" in rollout, "the rollout no longer has a clone-sync function"
    assert "SYNC=$(sync_clone)" in rollout, "sync_clone is defined but never called"


def test_the_clone_is_synced_before_the_container_is_touched(rollout: str) -> None:
    """Ordering is the safety property: sync the checkout, then deploy from it.

    The compose the rollout uses comes out of the clone, so syncing after compose_up would
    deploy from a stale recipe and only then update it — the opposite of the intent.
    """
    sync = rollout.index("SYNC=$(sync_clone)")
    # First compose call after the gate. There are two compose_up calls at runtime (the deploy
    # and the rollback); the assertion only needs one of them to be after the gate, and both are.
    up = rollout.index("compose_up", sync)
    pull = rollout.index('docker pull "$TARGET_IMAGE"', sync)
    assert sync < pull, "the clone is synced after the image is pulled"
    assert sync < up, "the clone is synced after compose is allowed to recreate the container"


def test_the_gate_runs_before_the_idempotent_skip(rollout: str) -> None:
    """The drift case is image-already-current and checkout-behind, so a skip first is a hole.

    On 13.10 every container ran the current image; the clones lagged. If the gate sat below the
    "already on target image" early-return, a rollout tick would see image == target, exit 0, and
    never sync the checkout — green while the drift check stayed red.
    """
    sync = rollout.index("SYNC=$(sync_clone)")
    skip = rollout.index("-> already on target image")
    assert sync < skip, (
        "the clone-sync gate is below the idempotent skip, so a rollout that is already on the "
        "target image skips it - which is exactly the drifted state it must catch"
    )


def test_the_clone_git_runs_as_its_owner(rollout: str) -> None:
    """The clones belong to deploy; the rollout runs as root. Git as root poisons the clone.

    A root-run fetch or pull leaves root-owned objects in the repository, and the next
    deploy-user git then fails with "insufficient permission for adding an object to repository
    database" — the failure sync-canon.sh documents at length. So every git call here is wrapped
    in `su ... $GIT_USER`.
    """
    body = _body(rollout, "sync_clone() {", "\n}")
    assert 'su -s /bin/bash "$GIT_USER"' in body, (
        "sync_clone runs git without dropping to the clone owner"
    )
    # fetch, pull and both rev-parse calls: four git invocations, all through su.
    assert body.count("su -s /bin/bash") == 4, (
        "not every git call in sync_clone drops to the clone owner"
    )
    assert 'GIT_USER="${GIT_USER:-deploy}"' in rollout, (
        "the clone owner is not defaulted to deploy"
    )


def test_the_clone_is_owned_before_the_fetch(rollout: str) -> None:
    """A poisoned object store makes deploy-user git fail, so ownership is normalised first.

    The 13.10 healing and the drift check both ran git as root and left root-owned loose objects
    (mode 600, e.g. 56b5541c) in the deploy-owned clones, so deploy-user git cannot even read
    them: the fetch fails and the gate that exists to heal the drift can never pass. The checkout
    is re-owned before any git call - but the container's bind-mounts are excluded, because
    data_preflight refuses a rollout whose data/ or backups/ is not on the runtime uid.
    """
    body = _body(rollout, "sync_clone() {", "\n}")
    own = body.index("-exec chown -R")
    fetch = body.index("fetch --quiet origin main")
    assert own < fetch, "the object store is re-owned after the fetch - the fetch fails on it"
    assert "-name data -prune" in body and "-name backups -prune" in body, (
        "the ownership pass does not exclude the container bind-mounts (data/, backups/)"
    )


def test_the_pull_is_fast_forward_only(rollout: str) -> None:
    """A diverged checkout must refuse, not have a side chosen for it.

    `git pull` without --ff-only would create a merge commit on a production clone — a silent
    history rewrite whose result nobody reviews. --ff-only makes divergence a refusal.
    """
    body = _body(rollout, "sync_clone() {", "\n}")
    assert "pull --ff-only --quiet origin main" in body, (
        "the pull command is not a fast-forward-only pull of origin/main"
    )


def test_a_clone_that_cannot_reach_origin_main_is_refused(rollout: str) -> None:
    """The gate's failure must be loud, abort the rollout, and tell the operator the fix."""
    gate = _body(rollout, "if ! SYNC=$(sync_clone); then", "\nfi")
    assert "send_alert critical" in gate, "a stale clone raises no alert"
    assert "HEAD ${head:0:7} != origin/main" in rollout, (
        "the gate never compares HEAD with origin/main, so it cannot catch the 13.10 drift"
    )
    assert "REFUSED" in gate, "the operator is not told the rollout was refused"
    assert "container untouched" in gate, "the refusal does not say the container was left alone"
    assert "exit 8" in gate, "a refused clone sync does not stop the rollout"
    # The refusal has to name the remedy, or it is just an error.
    assert "git -C $COMPOSE_DIR pull --ff-only" in gate, (
        "the refusal does not tell the operator how to sync the clone"
    )


def test_dry_run_reports_the_clone_sync(rollout: str) -> None:
    """A dry run that hides the gate it would enforce is not a preview of this rollout."""
    dry = _body(rollout, 'if [[ "$DRY" == 1 ]]; then', "exit 0")
    assert "clone" in dry and "git pull --ff-only" in dry, (
        "--dry-run does not report the clone-sync gate it would run"
    )
    assert "HEAD == origin/main" in dry, (
        "--dry-run does not state the condition the gate refuses on"
    )


# --- the sandbox grant the gate needs from the unit ----------------------------------------------


def _bots() -> list[str]:
    """The fleet, from the one place that iterates it.

    check_updates.sh's BOTS array decides which clones the gate will touch, so deriving the list
    from it means a tenth bot is covered the day it lands rather than the day someone remembers
    this test.
    """
    match = re.search(r"^BOTS=\((.*?)\)", CHECK_UPDATES_SH.read_text(), re.MULTILINE)
    assert match, "cannot find the BOTS array in check_updates.sh - the fleet list moved"
    bots = match.group(1).split()
    assert bots, "the BOTS array is empty"
    return bots


def _deploy_dir(rollout: str) -> str:
    match = re.search(r'^DEPLOY_DIR="([^"]+)"', rollout, re.MULTILINE)
    assert match, "cannot find DEPLOY_DIR in deploy_rollout.sh"
    return match.group(1)


def _granted(unit_text: str) -> set[str]:
    """Every path the unit's ReadWritePaths grants, with the '-' prefix stripped."""
    granted: set[str] = set()
    for line in unit_text.splitlines():
        if line.startswith("ReadWritePaths="):
            for part in line[len("ReadWritePaths=") :].split():
                granted.add(part.lstrip("-"))
    return granted


def _ungranted_clones(clones: list[str], granted: set[str]) -> list[str]:
    def allowed(path: str) -> bool:
        return path in granted or any(path.startswith(g.rstrip("/") + "/") for g in granted)

    return sorted(c for c in clones if not allowed(c))


def _clones(rollout: str) -> list[str]:
    return [f"{_deploy_dir(rollout)}/botkit-{bot}" for bot in _bots()]


def test_the_rollout_unit_may_write_every_clone_the_gate_syncs(rollout: str) -> None:
    """The gate writes the clone; the unit's sandbox must allow it, and the two must agree.

    sync_clone runs `git pull --ff-only` as the clone's owner and re-owns the tree, so a rollout
    writes /home/deploy/botkit-<bot>: git writes .git and, on a real update, the working tree, and
    chown rewrites the tree's metadata. The unit pins /home/deploy with ReadOnlyPaths, so every
    clone it intends to sync has to be handed back with its own ReadWritePaths line.

    On 16.10 the two disagreed. sync_clone started writing the clone on 15.10 while the unit still
    pinned /home/deploy whole, so every real rollout refused with EROFS and the unit sat FAILED,
    while the suite stayed green: the readwrite contract test only inspects write targets it can
    see as string literals (/var/log, /var/lib/botkit-*), and the clone path is computed from
    $DEPLOY_DIR, so it was never an input. verify_rollout.sh's dry run never writes the clone
    either. Finding it took running the unit, not reading it.
    """
    granted = _granted(UNIT.read_text())
    missing = _ungranted_clones(_clones(rollout), granted)
    assert not missing, (
        f"deploy_rollout.sh syncs {missing} but the rollout unit's ReadWritePaths does not grant "
        f"them: {sorted(granted)}. ProtectSystem=strict makes the clone read-only, sync_clone "
        "fails with EROFS, the gate refuses, and every rollout on the host stops - while a dry "
        "run, which never writes the clone, stays green."
    )


def test_the_clone_grant_check_can_fail() -> None:
    """The check above must be able to go red, or it is decoration.

    Mutation: /home/deploy is pinned read-only and only two of three clones are granted, so the
    ungranted one has to come back. A _ungranted_clones that returned [] for any input would let
    the positive test pass against a unit with no clone grants at all.
    """
    granted = {"/home/deploy/botkit-a", "/home/deploy/botkit-b"}
    clones = ["/home/deploy/botkit-a", "/home/deploy/botkit-b", "/home/deploy/botkit-c"]
    assert _ungranted_clones(clones, granted) == ["/home/deploy/botkit-c"]


def test_the_unit_still_pins_deploy_read_only_around_the_clones(rollout: str) -> None:
    """Opening the clones must not mean opening /home/deploy.

    The canon checkout the unit executes from lives under /home/deploy/botkit-monitoring; a bare
    ReadWritePaths=/home/deploy would hand a rollout write access to the script it is running.
    """
    deploy_dir = _deploy_dir(rollout)
    text = UNIT.read_text()
    read_only: set[str] = set()
    for line in text.splitlines():
        if line.startswith("ReadOnlyPaths="):
            read_only.update(part.lstrip("-") for part in line[len("ReadOnlyPaths=") :].split())
    assert deploy_dir in read_only, (
        f"{deploy_dir} is no longer pinned read-only; the clones must be opened one by one, not "
        "by widening the parent"
    )
    assert deploy_dir not in _granted(text), (
        f"ReadWritePaths grants all of {deploy_dir}, which includes the canon the unit runs from"
    )
