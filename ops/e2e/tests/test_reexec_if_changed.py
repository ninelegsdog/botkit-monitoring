"""The sync must run the code it just synced, not the code bash started with.

`sync-canon.sh` does `git reset --hard` and keeps executing, and bash reads a script
incrementally from its file. When the new commit changes sync-canon.sh itself, the rest
of the run is the old code while the clone holds the new one, so every step added in that
commit is skipped on the very sync that installs it. That happened on 14.11: the
Prometheus-reload step was merged, the sync printed `SYNC OK`, and the step never ran.

`ops/lib/reexec-if-changed.sh` is a library precisely so this can be tested without a
clone, root or docker: the tests feed it two files and check which one executed.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
HELPER = REPO / "ops" / "lib" / "reexec-if-changed.sh"

WRAPPER = """#!/usr/bin/env bash
. "$HELPER"
reexec_if_changed "$RUNNING" "$TARGET" "$@"
echo "AFTER-CONTINUE"
"""

TARGET = """#!/usr/bin/env bash
echo "TARGET-RAN rc=$# args=$*"
exit 7
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


def test_sync_canon_calls_the_helper_after_the_reset() -> None:
    source = (REPO / "ops" / "lib" / "sync-canon.sh").read_text()
    reset_at = source.index("reset --hard")
    call_at = source.index("reexec_if_changed")
    assert reset_at < call_at, "the re-exec must run after the clone was replaced"
    assert '"${BASH_SOURCE[0]}"' in source[call_at - 200 : call_at + 200]
