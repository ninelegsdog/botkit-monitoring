"""Contract: the TODO/FIXME gate fails on a real violation and passes on a referenced one.

The first version of `ops/lib/todo_check.sh` matched its own source and failed on every
run — a gate that is always red is a gate that gets deleted. This test pins the two
behaviours that matter: a comment without an issue number must fail the build, and the
same comment with `(#N)` must pass. It also asserts that prose mentioning TODO does not
trip the gate, because false positives are how such checks lose credibility.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
GATE = REPO / "ops" / "lib" / "todo_check.sh"


def _run(tree: pathlib.Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["TODO_SCAN_DIR"] = str(tree)
    return subprocess.run(
        ["bash", str(GATE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write(tree: pathlib.Path, name: str, body: str) -> None:
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_gate_fails_on_unreferenced_todo(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "lib/x.sh", "#!/usr/bin/env bash\n# TODO: wire this up\n")
    result = _run(tmp_path)
    assert result.returncode == 1, result.stdout
    assert "missing issue ref" in result.stdout


def test_gate_passes_on_referenced_todo(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "lib/x.sh", "#!/usr/bin/env bash\n# TODO(#123): wire this up\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "OK" in result.stdout


def test_gate_ignores_prose(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        "lib/x.py",
        '"""Explains why TODO markers must carry an issue number."""\n'
        'MESSAGE = "TODO without a number is not allowed"\n',
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout


def test_gate_ignores_its_own_source() -> None:
    result = _run(REPO / "ops")
    assert result.returncode == 0, result.stdout
