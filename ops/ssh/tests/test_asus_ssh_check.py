"""Smoke tests for ops/ssh/asus-ssh-check.sh."""

import os
import subprocess

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "asus-ssh-check.sh")


def test_script_exists_and_executable():
    assert os.path.isfile(SCRIPT)
    assert os.access(SCRIPT, os.X_OK)


def test_script_syntax():
    result = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
