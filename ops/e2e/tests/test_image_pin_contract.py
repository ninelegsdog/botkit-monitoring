"""Contract tests for where the running image comes from, added after the 03.10 incident.

The footgun this closes: every bot's compose file read

    image: ghcr.io/ninelegsdog/botkit-<bot>:${IMAGE_TAG:-main}

and nobody ever set IMAGE_TAG, because the rollout did not write it - it pinned the image in
a generated override file instead. So the steady state had no image pin in any file a human
or a compose run would look at, and `docker compose up -d` resolved the default and put the
bot on `:main`: an unpinned moving tag, no health gate, no record. The drift check noticed,
hours later, as a warning about a channel it could not explain.

The fix is one source of truth. IMAGE_TAG lives in the bot's env file, which compose already
reads through --env-file, so an operator's own command reproduces the deployed image. The
generated override is gone, and the compose default became a hard failure (`:?`) rather than
a guess - a deliberate `main` is still possible, but never by omission.

deploy_rollout.sh hardcodes DEPLOY_DIR, STATE_DIR and LOG, and making them overridable would
let a stray environment variable point a production rollout at the wrong tree, so these tests
read the shipped script and assert its invariants rather than running it end to end. They are
deliberately about the *shape* of the change: that the pin is written before the container is
replaced, that a rollback restores the tag that was actually recorded, and that the override
mechanism is gone rather than merely unused.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
ROLLOUT_SH = REPO / "ops" / "rollout" / "deploy_rollout.sh"
RESTORE_SH = REPO / "ops" / "backup" / "restore_bot.sh"
VERIFY_SH = REPO / "ops" / "rollout" / "verify_rollout.sh"
CHECK_UPDATES_SH = REPO / "ops" / "rollout" / "check_updates.sh"
UNIT = REPO / "ops" / "rollout" / "systemd" / "botkit-rollout-check.service"

BOT = "docuflow"


@pytest.fixture(scope="module")
def rollout() -> str:
    return ROLLOUT_SH.read_text()


@pytest.fixture(scope="module")
def restore() -> str:
    return RESTORE_SH.read_text()


def _body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


# --- the rollout script -------------------------------------------------------------------------


def test_the_tag_is_mandatory(rollout: str) -> None:
    """No default image. A forgotten argument used to deploy whatever `main` pointed at."""
    assert 'IMG="${IMG:-main}"' not in rollout, (
        "the rollout still falls back to main - that is the defect, not a convenience"
    )
    assert "if [[ -z \"$IMG\" ]]; then" in rollout, "a missing tag must be refused, not defaulted"
    # And the refusal has to say what to do, or it is just an error.
    refusal = _body(rollout, 'if [[ -z "$IMG" ]]; then', "exit 2")
    assert "image:tag is required" in refusal, "the refusal does not explain what is missing"
    assert "check_updates.sh" in refusal, "the refusal does not point at the caller that gets it right"


def test_the_pin_is_written_into_the_env_file(rollout: str) -> None:
    """IMAGE_TAG in the env file is the source compose reads. That is the whole point."""
    assert "set_image_tag()" in rollout, "the env file is no longer where the image is pinned"
    setter = _body(rollout, "set_image_tag() {", "read_image_tag()")
    assert "IMAGE_TAG=" in setter, "set_image_tag does not write IMAGE_TAG"
    assert "$ENV_FILE" in setter, "the tag is not written to the bot's env file"
    # Written back in place, not moved over the top: the env file holds REDIS credentials
    # and its mode and owner are load-bearing.
    assert 'cat "$tmp" >"$ENV_FILE"' in setter, (
        "the env file is replaced instead of rewritten - that changes its inode, mode and owner"
    )
    assert "chmod 600" in setter, "the rewritten env file loses its 600"


def test_a_tag_that_could_break_the_file_is_refused(rollout: str) -> None:
    """The tag is interpolated into sed and into a KEY=VALUE line, so it is validated."""
    setter = _body(rollout, "set_image_tag() {", "read_image_tag()")
    assert "A-Za-z0-9" in setter, "the tag is not validated before it reaches sed and the env file"
    assert "REFUSING tag" in setter, "a rejected tag is not reported"


def test_the_pin_is_written_before_the_container_is_replaced(rollout: str) -> None:
    """Ordering is the safety property: write the pin, then replace, never the reverse."""
    pin = rollout.index('log "bot=$bot deploying $TARGET_IMAGE ..."')
    set_tag = rollout.index('if ! set_image_tag "$IMG"; then', pin)
    # The first compose up after the deploy line, not the next one after set_image_tag: a
    # mutation that hoists compose_up above the pin is only visible if the search starts
    # from the deploy line, otherwise it finds the rollback's call and reports success.
    up = rollout.index("compose_up", pin)
    assert pin < set_tag < up, (
        "the image must be pinned in the env file before compose is allowed to recreate "
        "the container, or the container comes up on the previous pin"
    )


def test_a_failed_pin_stops_the_rollout(rollout: str) -> None:
    """If the env file cannot be written, the container must not be touched."""
    block = _body(rollout, 'if ! set_image_tag "$IMG"; then', "compose_up")
    assert "exit 7" in block, "a failed env write must abort with its own exit code"
    assert "REFUSED" in block, "the operator is not told the rollout was refused"
    assert "compose_up" not in block.split("exit 7")[1], "compose runs after the refusal"


def test_rollback_restores_the_recorded_tag_not_a_parsed_one(rollout: str) -> None:
    """PREV_TAG is read before anything is written.

    The old rollback passed the container's image reference back into compose. That breaks on
    a digest-pinned deployment, where there is no tag to parse, and it silently disagrees
    with the env file whenever the two had drifted apart.
    """
    prev = rollout.index("PREV_TAG=$(read_image_tag)")
    set_tag = rollout.index('set_image_tag "$IMG"', prev)
    assert prev < set_tag, (
        "the previous tag must be read before IMAGE_TAG is overwritten, or the rollback "
        "target is the tag we just replaced"
    )
    rollback = _body(rollout, 'if [[ "$HEALTH_RESULT" != "ok" ]]; then', 'printf ')
    assert 'set_image_tag "$PREV_TAG"' in rollback, "the rollback does not restore the previous tag"
    assert 'apply_image "$CUR_IMAGE"' not in rollback, "the rollback still goes through the old path"


def test_rollback_without_a_recorded_tag_is_refused_loudly(rollout: str) -> None:
    """No previous tag means no rollback. That has to be an alert, not a best guess."""
    rollback = _body(rollout, 'if [[ "$HEALTH_RESULT" != "ok" ]]; then', 'printf ')
    assert 'if [[ -z "$PREV_TAG" ]]; then' in rollback, "an empty PREV_TAG is not handled"
    block = _body(rollback, 'if [[ -z "$PREV_TAG" ]]; then', "exit 4")
    assert "send_alert critical" in block, "a bot that cannot be rolled back raises no alert"
    assert "cannot roll back" in block, "the refusal does not say what happened"


def test_the_generated_override_is_gone(rollout: str) -> None:
    """One source of truth. A leftover override silently wins over IMAGE_TAG on any compose
    run that includes it, which is what made the two disagree."""
    assert "apply_image" not in rollout, "the override-based apply path is still in the script"
    block = _body(rollout, 'if [[ -f "$OVERRIDE_FILE" ]]; then', "compose_up")
    assert "rm -f \"$OVERRIDE_FILE\"" in block, "a stale override is not cleaned up"
    assert "removed stale override" in block, "removing a stale override is not logged"


def test_dry_run_only_calls_functions_that_exist_by_then(rollout: str) -> None:
    """The dry-run block runs before the deploy flow, so anything it calls must be defined
    above it.

    This is not hypothetical: `read_image_tag` was defined below the dry-run, so the one
    command documented as safe to run printed `read_image_tag: command not found` and then
    reported the current pin as `none` - a dry run that confidently misreports the state it
    exists to show. Asserting that the dry-run *mentions* IMAGE_TAG was not enough; it says
    nothing about whether the name resolves when the block executes.
    """
    dry_at = rollout.index('if [[ "$DRY" == 1 ]]; then')
    dry = _body(rollout, 'if [[ "$DRY" == 1 ]]; then', "exit 0")
    called = set(re.findall(r"\$\((\w+)", dry))
    called |= set(re.findall(r"^(\w+)\(.*?\)", dry, re.M))
    assert called, "the dry run calls nothing, so this test proves nothing"
    for name in sorted(called):
        definition = rollout.find(f"{name}() {{")
        assert definition != -1, f"the dry run calls {name}(), which is never defined"
        assert definition < dry_at, (
            f"the dry run calls {name}() but it is defined further down the file, so the "
            "command printed 'command not found' while claiming to report the current state"
        )


UNIT_PATHS_KEY = "ReadWritePaths="


def _unit_paths(unit: str, key: str) -> set[str]:
    """Every path granted by a `Key=a b c` line."""
    found: set[str] = set()
    for line in unit.splitlines():
        if line.startswith(key):
            found.update(part for part in line[len(key):].split() if part)
    return found


def _env_dir(rollout: str) -> str:
    match = re.search(r'ENV_FILE="([^"]+)/\$bot\.env"', rollout)
    assert match, "cannot find where deploy_rollout.sh keeps the per-bot env file"
    assert match.group(1).startswith("/"), f"env path is not absolute: {match.group(1)}"
    return match.group(1)


def test_the_rollout_unit_may_write_the_directory_the_pin_lives_in() -> None:
    """The grant and the code must name the same directory, derived from both, not hardcoded.

    On 03.10 the pin moved into /usr/local/etc/botkit/<bot>.env and that path stayed in the unit's
    ReadOnlyPaths. Every rollout then aborted after `docker pull` and before touching the
    container, for all nine bots, and the fleet stopped updating while reporting healthy. The
    script and the unit each said something reasonable on their own; nobody compared them.

    So this reads the path out of the script and out of the unit and compares, rather than
    asserting a string that happened to be correct this morning.
    """
    env_dir = _env_dir(ROLLOUT_SH.read_text())
    granted = _unit_paths(UNIT.read_text(), UNIT_PATHS_KEY)
    assert env_dir in granted, (
        f"deploy_rollout.sh writes {env_dir} but ReadWritePaths does not grant it: "
        f"{sorted(granted)}. Every rollout would abort before recreating the container."
    )


def test_the_env_directory_is_not_also_listed_as_read_only() -> None:
    """Being in both lists works - ReadWritePaths is applied last - and is still a trap.

    The next person to read the file sees a path that is both writable and read-only and has no
    way to tell which statement is the intent. This test should answer that question.
    """
    env_dir = _env_dir(ROLLOUT_SH.read_text())
    read_only = _unit_paths(UNIT.read_text(), "ReadOnlyPaths=")
    assert env_dir not in read_only, (
        f"{env_dir} is in both ReadOnlyPaths and ReadWritePaths. It works today because systemd "
        "applies the latter last, but the file reads as a contradiction."
    )


def test_the_script_is_exercised_before_the_timer_can_use_it() -> None:
    """ExecStartPre, and the check has to include the write half.

    `bash -n` accepts a file in which a function is defined inside another function's body,
    because nothing is syntactically wrong with it: the function simply does not exist when the
    call executes. A misplaced brace did exactly that on 03.10 and 216 tests passed.

    A dry run was my first answer and it was not enough. It never writes, so it passed cleanly
    for an hour while every rollout in production failed on the write it never performed.
    """
    pre = [ln for ln in UNIT.read_text().splitlines() if ln.startswith("ExecStartPre=")]
    assert any("verify_rollout.sh" in ln for ln in pre), (
        f"ExecStartPre does not run verify_rollout.sh: {pre}"
    )

    verify = VERIFY_SH.read_text()
    assert "--dry-run" in verify, "the structural half of the check is gone"
    for needle in ("command not found", "unbound variable"):
        assert needle in verify, f"the check no longer looks for '{needle}'"

    # The half that matters: a real write of the real tag, through the real function.
    assert "set_image_tag_body" in verify, "the write check does not use the rollout's own function"
    assert "/^set_image_tag() {/,/^}/p" in verify, (
        "the write check no longer lifts set_image_tag out of the rollout script"
    )
    # The extracted body has to be what actually gets called. Asserting that the extraction exists
    # is not enough: replacing the eval with `eval "true"` leaves every other assertion in this
    # test passing while the write check quietly stops writing, which is the exact failure it
    # exists to catch.
    assert 'eval "$set_image_tag_body' in verify, (
        "the write check no longer calls the function it extracted - it evaluates something else"
    )
    # Definition and call must be two statements. `eval "$body '$tag'"` parses as a function
    # definition with an argument hanging off it, which is a syntax error - and the check then
    # reports every bot as unwritable, blocking the timer on a false alarm about a pin it never
    # even tried to write. bash -n on verify_rollout.sh does not see it either: the error only
    # appears at eval time.
    assert 'eval "$set_image_tag_body" || ! set_image_tag "$current"' in verify, (
        "the extracted function is no longer defined and then called as two statements"
    )
    assert "IMAGE_TAG changed from" in verify, (
        "the write check does not assert the file came back unchanged, so it could rewrite the "
        "pin while claiming to only prove writability"
    )
    assert "cannot have their image pin written" in verify, (
        "the write check does not fail the unit when a bot cannot be written"
    )


def test_the_environment_is_not_allowed_to_silence_the_check() -> None:
    """A non-zero exit with no shell error must warn, never block.

    A check that stops the timer because it cannot find a neighbouring directory becomes silent
    permanent staleness after about a month, and nobody notices. This fleet has already paid for
    that once.
    """
    verify = VERIFY_SH.read_text()
    assert 'elif [[ "$rc" -ne 0 ]]' in verify, (
        "the dry-run exit code is no longer distinguished from a shell error"
    )
    tail = verify.split('elif [[ "$rc" -ne 0 ]]', 1)[1][:400]
    assert "broken=1" not in tail, (
        "an environment-only failure now blocks the timer - see this test's docstring"
    )


def test_the_retry_guard_does_not_hide_the_failure_it_was_meant_to_dampen() -> None:
    """A bot that cannot be rolled out for 90 minutes is an incident, and the unit must say so.

    The guard existed to stop a failing rollout from being retried every 15 minutes. It worked -
    and it made the outage invisible: `rc_global` was only set on an actual rollout failure, so
    the tick after a failure skipped all nine bots and exited 0. The unit was green while the
    fleet was frozen, for one tick out of every ninety.
    """
    source = CHECK_UPDATES_SH.read_text()
    # Assert on the line itself. Slicing the surrounding block by the next "fi" is wrong: the
    # comment above this branch contains those letters inside ordinary words, so the block came
    # back truncated mid-comment - which is how this test could pass against a script that had no
    # rc_global in the branch at all.
    line = next((ln for ln in source.splitlines() if "still not rolled out" in ln), None)
    assert line is not None, "the guard branch no longer says what it is doing"
    assert "rc_global=1" in line, (
        "the retry-guard skip sets no failure code, so the unit goes green on exactly the ticks "
        "that matter most"
    )
    assert "attempted_still_recent" in source, "the guard is gone entirely"


def test_set_image_tag_touches_nothing_but_the_tag() -> None:
    """That file holds the Redis credentials. The function rewrites one line and stops."""
    rollout = ROLLOUT_SH.read_text()
    body = re.search(r"set_image_tag\(\) \{(.*?)\n\}", rollout, re.S)
    assert body, "cannot find set_image_tag() in deploy_rollout.sh"
    text = body.group(1)

    # Two writes into the temp file are legitimate: replace the line, append when the key is
    # absent. A blanket sed -i, or a third write, would be the failure mode.
    # The redirect target is >"$tmp" - with the quotes - so counting ">$tmp" matches nothing and
    # the assertion passes for the wrong reason on a function that writes nothing at all.
    writes = text.count('>"$tmp"')
    assert writes == 2, (
        f"set_image_tag performs {writes} writes into the temp file; expected the replace-line "
        "and the append-if-absent, nothing else"
    )
    assert 'cat "$tmp" >"$ENV_FILE"' in text, (
        "the env file must be rewritten in place, not replaced by a move - its mode and owner "
        "are load-bearing and it holds credentials"
    )
    assert "sed -i" not in text, "sed -i replaces the file and loses the inode, mode and owner"
    # Count commands, not the word: the function's own comment explains that the tag is
    # "interpolated into sed", so a plain text count finds three and reports a false violation.
    sed_commands = [
        ln.strip() for ln in text.splitlines() if ln.strip().startswith("sed ")
    ]
    assert len(sed_commands) == 1, (
        f"set_image_tag runs {len(sed_commands)} sed commands, expected exactly one - more than "
        "one means more than the tag line could be rewritten: "
        f"{sed_commands}"
    )


def test_dry_run_reports_both_preflights(rollout: str) -> None:
    """A dry run that hides what it would change is worse than no dry run."""
    dry = _body(rollout, 'if [[ "$DRY" == 1 ]]; then', "exit 0")
    assert "data_preflight" in dry, "--dry-run no longer reports the data-layer preflight"
    assert "IMAGE_TAG=" in dry, (
        "--dry-run does not say which tag would be pinned where - the change it exists to "
        "preview is exactly the one thing it stays quiet about"
    )


# --- restore ------------------------------------------------------------------------------------


def test_restore_does_not_append_the_obsolete_override(restore: str) -> None:
    """Two ways to pin an image is the defect. restore used to honour both."""
    assert 'compose_files+=(-f "$OVERRIDE_DIR/$name.yml")' not in restore, (
        "restore still picks up the generated override, so it can restore an image that is "
        "not the one IMAGE_TAG names"
    )
    assert "IMAGE_TAG" in restore, "restore does not say where the image comes from now"


def test_the_compose_array_is_built_correctly(restore: str) -> None:
    """`"${compose_files[@]}}"` concatenated a literal brace onto the last element.

    Harmless while the array had one element and fatal as soon as it had two: compose was
    handed `support.yml}` and refused to start. The script whose entire job is to restore a
    database silently did not work on exactly the bots that had a leftover override.
    """
    match = re.search(r"DC=\((.*?)\)\n", restore)
    assert match, "cannot find the DC array assignment"
    assert '"${compose_files[@]}}"' not in match.group(1), (
        "the trailing brace is back: compose will receive a filename ending in }"
    )
    assert '"${compose_files[@]}"' in match.group(1), "DC must expand the array as an array"


def test_a_leftover_override_is_reported_rather_than_ignored_silently(restore: str) -> None:
    """If a file is not used, say so. Silence reads as "everything is configured"."""
    assert "WARN" in restore, "restore does not mention the obsolete override at all"
    assert "устаревший override" in restore, "restore does not tell the operator what to do about it"
