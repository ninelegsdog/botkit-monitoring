"""Alert-lifetime contract: every producer names when its own alert ends.

A producer that POSTs a payload without `endsAt` hands the alert's lifetime to
Alertmanager, which applies its own `resolve_timeout` (5m). The producer re-fires on its
own tick (15-60m), so the alert is absent most of the time and a still-broken condition
reads as resolved between attempts. The rollout alert that went missing on 16.10 was
exactly this: the script logged `alert sent (critical)` and `/api/v2/alerts` was empty.

Two properties, per producer:

  * `endsAt` is present, so we own how long the alert stays firing;
  * the HTTP status is checked, so a 4xx (Alertmanager rejecting the payload) is not
    logged as "sent" - curl exits 0 on a 400, and `curl -s … && log sent` printed
    "alert sent" for alerts that never landed.

The assertions look at the payload, not at prose: the shell half reads `send_alert` out
of each script and ignores comment lines, and the Python half reads the string keys of the
dict the function actually builds. A comment or docstring that merely says "endsAt" does
not satisfy either - which the mutation in the docstring of each test is there to keep
true. The self-check at the end proves the extractor finds something to look at.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

# This file lives in ops/e2e/tests/: parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]

# Every script that pushes to Alertmanager. A new producer must appear here, or its alert
# lifetime is decided by Alertmanager and nothing says so.
SHELL_PUSHERS = [
    "ops/rollout/deploy_rollout.sh",
    "ops/drift/check_drift.sh",
    "ops/drift/otlp-e2e.sh",
    "ops/e2e/smoke_all.sh",
    "ops/backup/check_backups.sh",
]

# Python producers, mapped to the functions that build each Alertmanager payload:
# webhook_check builds its firing and resolving payloads inline; run_e2e builds its
# firing payload in build_alert and send_alert only posts it.
PY_PAYLOAD_FUNCS = {
    "ops/e2e/webhook_check.py": ["send_alert", "resolve_alert"],
    "ops/e2e/e2e/run_e2e.py": ["build_alert"],
}


def _bash_function(text: str, name: str) -> str:
    """Return a top-level bash function, from `name() {` to the closing `}`.

    The scripts in this repository close top-level functions with a `}` at column zero,
    which is what lets a line scan stand in for a shell parser.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{name}\(\)\s*\{{", line):
            start = i
            break
    assert start is not None, f"no {name}() in this script"
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            return "\n".join(lines[start : j + 1])
    raise AssertionError(f"{name}() has no closing brace at column zero")


def _shell_code(text: str) -> str:
    """Drop whole-line comments, so an assertion on a token cannot be met by prose."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _find_function(src: str, name: str) -> ast.FunctionDef | None:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _dict_string_keys(src: str, name: str) -> set[str]:
    """The string keys of every dict literal inside `name`, docstrings and comments excluded."""
    node = _find_function(src, name)
    assert node is not None, f"no {name}() in this module"
    keys: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Dict):
            for key in inner.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


@pytest.mark.parametrize("rel", SHELL_PUSHERS)
def test_shell_alert_names_its_own_end(rel):
    """endsAt must be set, or Alertmanager's 5m resolve_timeout owns the alert's life.

    Mutation: delete the `"endsAt"` key from the payload - this goes red while the script
    still posts (the old behaviour), which is the defect.
    """
    body = _shell_code(_bash_function((REPO / rel).read_text(), "send_alert"))
    assert "endsAt" in body, f"{rel}: send_alert posts no endsAt, so Alertmanager decides the lifetime"
    assert "startsAt" in body, f"{rel}: send_alert posts no startsAt"


@pytest.mark.parametrize("rel", SHELL_PUSHERS)
def test_shell_alert_checks_the_http_status(rel):
    """curl exits 0 on a 400, so the exit code alone logs a rejected alert as sent.

    Mutation: drop the `-w '%{http_code}'` capture and judge on curl's exit code alone
    (the old `curl … && log sent`) - this goes red.
    """
    body = _shell_code(_bash_function((REPO / rel).read_text(), "send_alert"))
    assert "%{http_code}" in body, f"{rel}: send_alert logs sent without checking the HTTP status"


@pytest.mark.parametrize("rel", sorted(PY_PAYLOAD_FUNCS))
def test_python_alert_names_its_own_end(rel):
    """The Python producers must set endsAt too, for the same reason as the shell half.

    `resolve_alert` is included on purpose: its payload is the closing one, and it needs a
    real window - Alertmanager rejects `startsAt == endsAt` ("start time must be before end
    time"), which is how a recovered bot's alert used to stay firing. Its behaviour is
    pinned in test_webhook_runner; here it is pinned at the source. Reads dict keys, so the
    docstring in webhook_check.send_alert that explains endsAt cannot satisfy it.
    """
    src = (REPO / rel).read_text()
    for fn in PY_PAYLOAD_FUNCS[rel]:
        keys = _dict_string_keys(src, fn)
        assert "endsAt" in keys, f"{rel}: {fn} builds a payload with no endsAt key"
        assert "startsAt" in keys, f"{rel}: {fn} builds a payload with no startsAt key"


def test_the_shell_extractor_finds_a_known_function():
    """Self-check: a guard that finds nothing is a green light."""
    sample = "#!/usr/bin/env bash\nsend_alert() {\n  echo hi\n}\nother() {\n  :\n}\n"
    body = _bash_function(sample, "send_alert")
    assert "echo hi" in body and "other" not in body, "the function extractor stopped working"


def test_the_shell_code_filter_drops_comments():
    """Self-check: `endsAt` in a comment must not read as a payload key."""
    sample = "send_alert() {\n  # endsAt was missing here\n  :\n}\n"
    assert "endsAt" not in _shell_code(sample), "comment stripping stopped working"


def test_the_python_key_reader_ignores_prose():
    """Self-check: a docstring mentioning endsAt is not a dict key."""
    src = 'def send_alert():\n    """posts endsAt."""\n    d = {"labels": {}}\n    return d\n'
    keys = _dict_string_keys(src, "send_alert")
    assert "endsAt" not in keys and "labels" in keys, "the dict-key reader reads prose"
