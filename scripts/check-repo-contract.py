#!/usr/bin/env python3
"""Check a repository against the shared contract.

Standard library only, no third-party dependency, so it runs anywhere Python
does -- including a fresh CI runner before anything is installed.

Exit codes
    0  contract satisfied
    1  contract violated -- every violation is printed, not just the first
    2  the check could not verify anything. This is NOT success: a validator
       that cannot reach the contract must not report green, because green
       from a check that did not run is worse than a red one you can see.

The checks that can silently pass are the reason this file exists. Every rule
below has a negative test in ``check-repo-contract_test.py``; a rule that has
never been seen to fail is an assumption wearing the costume of a test.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_CANNOT_VERIFY = 0, 1, 2


def slugify(heading: str) -> str:
    """GitHub's anchor algorithm, as far as it matters for a Contents block."""
    h = re.sub(r"[`*_]", "", heading).strip().lower()
    h = re.sub(r"[^\w\s-]", "", h, flags=re.UNICODE)
    return re.sub(r"\s+", "-", h)


# Named rather than inline: a bare 0.20 in a comparison reads as a typo waiting
# to happen, and these are the two numbers the whole language rule turns on.
MIN_LETTERS_FOR_LANGUAGE_RULE = 200
CYRILLIC_SHARE_THRESHOLD = 0.20
# argv[0] + repository root + contract path.
ARITY = 3
GOVERNANCE_DOCS = frozenset({
    "README.md", "CHANGELOG.md", "LICENSE", "CONTRIBUTING.md",
    "SECURITY.md", "CODE_OF_CONDUCT.md", "AGENTS.md",
})


def is_cyrillic_heavy(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < MIN_LETTERS_FOR_LANGUAGE_RULE:
        return False
    cyr = sum(1 for c in letters if "CYRILLIC" in unicodedata.name(c, ""))
    return cyr / len(letters) > CYRILLIC_SHARE_THRESHOLD


class Report:
    def __init__(self) -> None:
        self.violations: list[str] = []
        self.checked = 0

    def fail(self, rule: str, detail: str) -> None:
        self.violations.append(f"[{rule}] {detail}")

    def ok(self) -> None:
        self.checked += 1


def load_contract(contract_path: Path) -> dict:
    if not contract_path.is_file():
        raise SystemExit(f"contract not found: {contract_path}")
    return json.loads(contract_path.read_text(encoding="utf-8"))


def repo_type(contract: dict, repo: str) -> str | None:
    t = contract.get("repos", {}).get(repo)
    if t is None:
        raise SystemExit(f"repository {repo!r} has no type in the contract")
    if t not in contract.get("types", {}):
        raise SystemExit(f"repository {repo!r} has unknown type {t!r}")
    return t


def check_required(root: Path, spec: dict, rep: Report) -> None:
    for rel in spec["required"]:
        if (root / rel).exists():
            rep.ok()
        else:
            rep.fail("required-artifact", f"missing required file: {rel}")
    check_exemptions(root, spec, rep)


def check_exemptions(root: Path, spec: dict, rep: Report) -> None:
    """An exemption must be an exemption, not a silent absence.

    Kept separate from the required-artifact pass so each function stays small
    enough to read in one go.
    """
    for rel, reason in spec.get("exemptions", {}).items():
        if (root / rel).exists():
            rep.fail(
                "exemption-violated",
                f"{rel} is exempt for this type ({reason}) but is present",
            )
        else:
            rep.ok()


def check_readme(root: Path, rep: Report, policy: dict | None = None) -> None:
    """Front-page rules: language, Contents, badges, leftover placeholders."""
    p = root / "README.md"
    if not p.is_file():
        rep.fail("readme", "README.md is missing")
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    check_language(text, rep, policy)
    check_contents(text, rep)
    check_badges(root, text, rep)
    check_placeholders(text, rep)


def check_language(text: str, rep: Report, policy: dict | None) -> None:

    waived = policy.get("language_policy", {}).get("waived") if policy else None
    if is_cyrillic_heavy(text):
        if waived:
            # An override must still be an override someone can see. The rule is
            # not silently disabled: it reports, and it names the reason.
            print(f"  note: language policy waived for this repository -- {waived}")
            rep.ok()
        else:
            rep.fail(
                "language-policy",
                "README.md is predominantly Cyrillic; the front page must be English "
                "(long-form docs/ stay in Russian)",
            )
    else:
        rep.ok()

def check_contents(text: str, rep: Report) -> None:
    headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE)
    anchors = {slugify(h) for h in headings}

    # Contents links must point at headings that exist.
    in_toc = False
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s+Contents\s*$", line):
            in_toc = True
            continue
        if in_toc and re.match(r"^#{1,6}\s+", line):
            in_toc = False
        if in_toc:
            for target in re.findall(r"\]\(#([^)]+)\)", line):
                if target not in anchors:
                    rep.fail("toc-anchor", f"Contents links to #{target}, which no heading defines")
                else:
                    rep.ok()

    if not re.search(r"^#{1,6}\s+Contents\s*$", text, re.MULTILINE):
        rep.fail("toc", "README.md has no Contents section")
    else:
        rep.ok()

def check_badges(root: Path, text: str, rep: Report) -> None:
    # A CI badge in a repository with no CI is a badge that can only ever be red
    # or absent -- it advertises a check that is not running.
    wanted = ["github/license"]
    if (root / ".github" / "workflows").is_dir() and any(
            (root / ".github" / "workflows").glob("*.y*ml")):
        wanted.append("actions/workflows/ci.yml/badge.svg")
    for badge in wanted:
        if badge not in text:
            rep.fail("badges", f"README.md lacks the {badge.split('/')[0]} badge")
        else:
            rep.ok()

def check_placeholders(text: str, rep: Report) -> None:
    # Placeholders are how a generated document ships a lie.
    # Inline code is syntax, not prose: `botkit-farm study <repo>` documents an
    # argument, while `install from <repo>` is an unfilled placeholder. Only the
    # second is a defect, so the first is excluded before the rule runs.
    prose = re.sub(r"`[^`\n]*`", "", text)
    for pat, label in (
        (r"\{\{\s*\w+.*?\}\}", "template placeholder {{ }}"),
        (r"<(?:repo|project|your[-_ ]?\w+)>", "unfilled angle placeholder"),
        (r"\bTODO\b|\bFIXME\b|\bXXX\b", "unresolved TODO/FIXME"),
        (r"\bLorem ipsum\b", "lorem ipsum"),
    ):
        hit = re.search(pat, prose)
        if hit:
            rep.fail("placeholder", f"README.md contains a {label}: {hit.group(0)!r}")
        else:
            rep.ok()


def check_links(root: Path, rep: Report) -> None:
    """Every relative link in the documentation must resolve."""
    docs = [root / "README.md"]
    if (root / "docs").is_dir():
        docs += sorted((root / "docs").rglob("*.md"))
    for doc in docs:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        # Fenced code is not prose; a link inside it is documentation of a link.
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for m in re.finditer(r"\[[^\]]*\]\((?!https?://|mailto:|#)([^)\s]+)", text):
            target = m.group(1).split("#")[0]
            if not target:
                continue
            if not (doc.parent / target).resolve().exists():
                rep.fail("broken-link", f"{doc.relative_to(root)} links to {target}, which does not exist")
            else:
                rep.ok()


def check_orphans(root: Path, rep: Report) -> None:
    """A document nobody links to is a document nobody reads."""
    readme = (root / "README.md")
    text = readme.read_text(encoding="utf-8", errors="replace") if readme.is_file() else ""
    # Root documents the contract requires are linked by definition: a
    # CONTRIBUTING.md that no page points at is still reachable from the
    # repository root, which is where a reader looks for it.
    for p in sorted(root.glob("*.md")):
        if p.name in GOVERNANCE_DOCS:
            continue
        if p.name not in text:
            rep.fail("orphan-doc", f"{p.name} is not linked from README.md")
        else:
            rep.ok()


def check_coc_single_home(root: Path, rep: Report) -> None:
    if (root / "CODE_OF_CONDUCT.md").exists() and (root / ".github" / "CODE_OF_CONDUCT.md").exists():
        rep.fail("coc-duplicate",
                 "CODE_OF_CONDUCT.md exists both at the root and under .github/; "
                 "GitHub reads only the root one")
    else:
        rep.ok()


def check_secrets(root: Path, rep: Report) -> None:
    """A committed .env is the one failure this validator must never miss."""
    if (root / ".env").exists():
        rep.fail("secret", ".env is committed; secrets must live in .env.example only")
    else:
        rep.ok()
    gi = root / ".gitignore"
    if not gi.is_file():
        rep.fail("gitignore", ".gitignore is missing")
    elif re.search(r"^\.env\s*$", gi.read_text(encoding="utf-8", errors="replace"), re.MULTILINE):
        rep.ok()
    else:
        rep.fail("gitignore", ".gitignore does not ignore .env")


def main() -> int:
    if len(sys.argv) < ARITY:
        print(f"usage: {Path(sys.argv[0]).name} <repo-root> <contract.json>", file=sys.stderr)
        return EXIT_CANNOT_VERIFY
    root = Path(sys.argv[1]).resolve()
    contract = load_contract(Path(sys.argv[2]))
    repo = root.name
    if not root.is_dir():
        print(f"repository root not found: {root}", file=sys.stderr)
        return EXIT_CANNOT_VERIFY

    t = repo_type(contract, repo)
    spec = contract["types"][t]
    rep = Report()

    check_required(root, spec, rep)
    check_readme(root, rep, contract.get("policy_overrides", {}).get(repo))
    check_links(root, rep)
    check_orphans(root, rep)
    check_coc_single_home(root, rep)
    check_secrets(root, rep)

    if rep.checked == 0 and not rep.violations:
        print(f"{repo}: nothing was checked -- refusing to report success", file=sys.stderr)
        return EXIT_CANNOT_VERIFY

    if rep.violations:
        print(f"{repo} [{t}]: {len(rep.violations)} contract violation(s)")
        for v in rep.violations:
            print(f"  {v}")
        return EXIT_FAIL
    print(f"{repo} [{t}]: contract satisfied ({rep.checked} assertions)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
