"""The sync must run the code it just synced, not the code bash started with.

`sync-canon.sh` does `git reset --hard` and keeps executing. When the new commit changes
sync-canon.sh itself, the rest of the run is the old code while the clone holds the new one,
so every step added in that commit is skipped on the very sync that installs it. That
happened on 14.11: the Prometheus-reload step was merged, the sync printed `SYNC OK`, and
the step never ran.

The first version of the helper took the *path* of the running script and hashed it after
the reset. By then that path is the synced file, so the two hashes were always equal and the
guard never fired — it compared a file with itself. The regression test below is that exact
shape: one path, mutated between the hash capture and the call.

`ops/lib/reexec-if-changed.sh` is a library precisely so this can be tested without a
clone, root or docker: the tests feed it files and check which bytes executed.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
HELPER = REPO / "ops" / "lib" / "reexec-if-changed.sh"

# The real call shape: hash taken at the top of the script, before anything replaces it.
WRAPPER = """#!/usr/bin/env bash
SELF_HASH=$(sha256sum "$RUNNING" | awk '{print $1}')
. "$HELPER"
reexec_if_changed "$SELF_HASH" "$TARGET" "$@"
echo "AFTER-CONTINUE"
"""

TARGET = """#!/usr/bin/env bash
echo "TARGET-RAN rc=$# args=$*"
exit 7
"""

# One file replaced on disk between the hash and the call, by a child process - the shape of
# `git reset --hard`, which is what actually happens in the sync. The replacement has to be a
# rename, not a truncating write: git unlinks and renames, so the running bash keeps reading
# the old inode and continues, which is exactly why the stale code finishes the run. A cp over
# the same inode would make bash hit EOF and exit silently, testing bash instead of the guard.
SELF_MUTATING = """#!/usr/bin/env bash
SELF="$0"
SELF_HASH=$(sha256sum "$SELF" | awk '{print $1}')
bash -c 'mv "$NEW_BODY" "$SELF_PATH"'
. "$HELPER"
reexec_if_changed "$SELF_HASH" "$SELF"
echo "AFTER-CONTINUE"
"""

NEW_BODY = """#!/usr/bin/env bash
echo "NEW-CODE-RAN"
exit 9
"""


class Harness:
    def __init__(self, tmp_path: pathlib.Path) -> None:
        self.tmp_path = tmp_path
        self.running = tmp_path / "sync-canon.sh"
        self.running.write_text(WRAPPER)
        self.target = tmp_path / "synced" / "sync-canon.sh"
        self.target.parent.mkdir()

    def run(self, target_body: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        self.target.write_text(target_body)
        return subprocess.run(
            ["bash", str(self.running), *args],
            env={
                **os.environ,
                "HELPER": str(HELPER),
                "RUNNING": str(self.running),
                "TARGET": str(self.target),
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )


def test_identical_files_continue_in_the_running_script(tmp_path: pathlib.Path) -> None:
    result = Harness(tmp_path).run(WRAPPER)
    assert "AFTER-CONTINUE" in result.stdout, result.stdout + result.stderr
    assert "TARGET-RAN" not in result.stdout


def test_a_changed_target_is_executed_with_the_same_arguments(tmp_path: pathlib.Path) -> None:
    result = Harness(tmp_path).run(TARGET, "deadbeef")
    assert "TARGET-RAN rc=1 args=deadbeef" in result.stdout, result.stdout + result.stderr
    assert "AFTER-CONTINUE" not in result.stdout, "the old script kept running"
    assert result.returncode == 7, "the synced copy's exit code must be the run's exit code"


def test_the_reexec_flag_stops_a_loop(tmp_path: pathlib.Path) -> None:
    """The child inherits the flag; without it a mismatch would re-exec forever."""
    result = Harness(tmp_path).run(TARGET, "deadbeef", SYNC_CANON_REEXEC="1")
    assert "TARGET-RAN" not in result.stdout
    assert "AFTER-CONTINUE" in result.stdout


def test_a_missing_target_is_not_fatal(tmp_path: pathlib.Path) -> None:
    """A scratch copy without ops/lib must not make the sync die on a helper."""
    h = Harness(tmp_path)
    h.target.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", str(h.running)],
        env={**os.environ, "HELPER": str(HELPER), "RUNNING": str(h.running), "TARGET": str(h.target)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "AFTER-CONTINUE" in result.stdout


def test_a_file_rewritten_in_place_is_re_executed(tmp_path: pathlib.Path) -> None:
    """The 14.11 defect: the running path and the target are the same file.

    Hashing that path after the reset compares the file with itself, so the guard is a
    no-op. Only a hash taken before the replacement can see the change.
    """
    script = tmp_path / "self-mutating.sh"
    script.write_text(SELF_MUTATING)
    new_body = tmp_path / "new-body.sh"
    new_body.write_text(NEW_BODY)
    result = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "HELPER": str(HELPER), "NEW_BODY": str(new_body),
                "SELF_PATH": str(script)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "NEW-CODE-RAN" in result.stdout, result.stdout + result.stderr
    assert "AFTER-CONTINUE" not in result.stdout, "the old bytes kept running after the rewrite"
    assert result.returncode == 9


def test_sync_canon_hashes_itself_before_the_reset() -> None:
    source = (REPO / "ops" / "lib" / "sync-canon.sh").read_text()
    hash_at = source.index("SELF_HASH=$(sha256sum")
    reset_at = source.index("reset --hard origin/main")
    call_at = source.index("reexec_if_changed")
    assert hash_at < reset_at, "the hash must be taken before the clone replaces the file"
    assert reset_at < call_at, "the re-exec must run after the clone was replaced"
    assert '"$SELF_HASH"' in source[call_at - 200 : call_at + 200], (
        "the helper is called with a path instead of the pre-reset hash"
    )
