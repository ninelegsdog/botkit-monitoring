#!/usr/bin/env python3
"""Negative tests for check-repo-contract.py.

A validator that has only ever returned green has not been tested. It has been
exercised. This builds a synthetic repository that satisfies the contract, then
breaks it one rule at a time and asserts the validator notices -- and names the
right rule. A rule that cannot be made to fail is a rule that is not checked.

    python3 check-repo-contract_test.py

Exit 0 when every rule is proven live. Exit 1 when one is not.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VALIDATOR = HERE / "check-repo-contract.py"
# Inside a repository the contract slice sits next to the scripts. At the
# authoring tree it sits one level up. Accept either, and say which, rather than
# failing with "cannot verify" and leaving the reader to guess why.
def find_contract() -> Path:
    for cand in (HERE / "repo-contract.json", HERE.parent / "contract.json"):
        if cand.is_file():
            return cand
    return HERE / "repo-contract.json"


CONTRACT = find_contract()

GOOD_README = """# Delivery

An example repository used to prove the contract checker fails when it should.

[![CI](https://github.com/ninelegsdog/botkit-delivery/actions/workflows/ci.yml/badge.svg)](x)
[![License](https://img.shields.io/github/license/ninelegsdog/botkit-delivery.svg)](LICENSE)

## Contents

- [Quick start](#quick-start)
- [Extra](#extra)

## Quick start

Run it. See [docs/testing.md](docs/testing.md) for more, and the [testing notes](README-testing.md).

## Extra

Nothing.
"""


def build_good(root: Path, kind: str = "telegram-bot") -> None:
    """A repository that satisfies the contract for `kind`."""
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    spec = contract["types"][kind]
    for rel in spec["required"]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("placeholder\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "testing.md").write_text(
        "# Testing\n\nSee also [testing notes](../README-testing.md).\n", encoding="utf-8")
    (root / "README-testing.md").write_text("# Testing notes\n", encoding="utf-8")
    # A workflow directory, so the CI badge is a live requirement. Without it the
    # validator correctly stops asking for a badge advertising a check that is
    # not running -- and the badge rule would silently lose its negative test.
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    (root / "README.md").write_text(GOOD_README, encoding="utf-8")
    # .gitignore must actually ignore .env, and .env must not exist.
    (root / ".gitignore").write_text(".env\n__pycache__/\n", encoding="utf-8")


def run(root: Path) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, str(VALIDATOR), str(root), str(CONTRACT)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return p.returncode, p.stdout + p.stderr


def case(name: str, mutate, expect_rule: str | None = None,
         kind: str = "telegram-bot", repo: str = "botkit-delivery") -> bool:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / repo
        root.mkdir()
        build_good(root, kind)
        mutate(root)
        code, out = run(root)
        if code != 1:
            print(f"FAIL {name}: expected exit 1, got {code}")
            print("       " + out.strip().replace("\n", "\n       "))
            return False
        if expect_rule and f"[{expect_rule}]" not in out:
            print(f"FAIL {name}: expected rule {expect_rule!r} to fire; output was:")
            print("       " + out.strip().replace("\n", "\n       "))
            return False
        print(f"ok   {name}")
        return True


def main() -> int:
    if not VALIDATOR.is_file() or not CONTRACT.is_file():
        print("cannot verify: validator or contract missing", file=sys.stderr)
        return 2

    # Control: the synthetic repository must be genuinely clean, or every
    # negative case below would "pass" for the wrong reason.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "botkit-delivery"
        root.mkdir()
        build_good(root)
        code, out = run(root)
        if code != 0:
            print("FAIL control: a repository built to satisfy the contract was rejected")
            print("       " + out.strip())
            return 1
        print("ok   control: clean repository is accepted")

    results = [
        case("required artifact deleted", lambda r: (r / "SECURITY.md").unlink(), "required-artifact"),
        case("required issue form deleted",
             lambda r: (r / ".github/ISSUE_TEMPLATE/bug-report.yml").unlink(),
             "required-artifact"),
        case("exempt file present in a type that exempts it",
             lambda r: shutil.copy(VALIDATOR, r / "Dockerfile"), "exemption-violated",
             kind="library", repo="botkit-core"),
        case("broken relative link",
             lambda r: r.joinpath("README.md").write_text(
                 r.joinpath("README.md").read_text().replace(
                     "[docs/testing.md](docs/testing.md)", "[gone](docs/gone.md)"), encoding="utf-8"),
             "broken-link"),
        case("dead Contents anchor",
             lambda r: r.joinpath("README.md").write_text(
                 r.joinpath("README.md").read_text().replace(
                     "- [Extra](#extra)", "- [Extra](#no-such-heading)"), encoding="utf-8"),
             "toc-anchor"),
        case("orphan document",
             lambda r: r.joinpath("orphan.md").write_text("# Orphan\n", encoding="utf-8"), "orphan-doc"),
        case("Cyrillic front page",
             lambda r: r.joinpath("README.md").write_text(
                 "# Доставка\n\n" + ("Бот отслеживания доставки. " * 40) + "\n", encoding="utf-8"),
             "language-policy"),
        case("template placeholder left in",
             lambda r: r.joinpath("README.md").write_text(
                 r.joinpath("README.md").read_text().replace("## Extra", "## Extra\n\n{{ project_slug }}"),
                 encoding="utf-8"), "placeholder"),
        case("TODO left in",
             lambda r: r.joinpath("README.md").write_text(
                 r.joinpath("README.md").read_text().replace("## Extra", "## Extra\n\nTODO: write this"),
                 encoding="utf-8"), "placeholder"),
        case("CI badge removed",
             lambda r: r.joinpath("README.md").write_text(
                 r.joinpath("README.md").read_text().replace(
                     "actions/workflows/ci.yml/badge.svg", "example/badge.svg"), encoding="utf-8"),
             "badges"),
        case("Contents section removed",
             lambda r: r.joinpath("README.md").write_text(
                 "# x\n\nno toc here\n" + "filler " * 60, encoding="utf-8"), "toc"),
        case("committed .env",
             lambda r: r.joinpath(".env").write_text("BOT_TOKEN=x\n", encoding="utf-8"), "secret"),
        case(".gitignore does not ignore .env",
             lambda r: r.joinpath(".gitignore").write_text("__pycache__/\n", encoding="utf-8"), "gitignore"),
        case("CoC in two homes",
             lambda r: shutil.copy(r / "CODE_OF_CONDUCT.md", r / ".github/CODE_OF_CONDUCT.md"),
             "coc-duplicate"),
    ]

    print(f"\n{sum(results)}/{len(results)} rules proven live")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
