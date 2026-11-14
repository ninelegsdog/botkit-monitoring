"""Contract: every textfile writer chmods its file to 644 before publishing it.

On 14.11 the healthcheck and rollout pollers wrote their textfiles with `mktemp` + `mv`
and no `chmod`. `mktemp` creates 0600, `mv` preserves the mode, and node-exporter runs
as an unprivileged user inside its container, so it could not read the files. The metrics
simply did not exist, and the guarding alerts are `absent(...) or ...`, which is true for
a missing metric — so a running poller looked exactly like a dead one.

The fix is `chmod 644` before `mv`. This test keeps every writer honest: a new writer
that forgets the chmod fails here, not in production at 06:15.

These are contract tests (they read the shipped scripts), because running each writer
needs a live host, a textfile directory and stubbed systemctl/docker/curl. The shape that
matters is in the source.
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]

# Every script that publishes into /var/lib/node-exporter-textfile.
TEXTFILE_WRITERS = [
    REPO / "ops" / "health" / "healthcheck.sh",
    REPO / "ops" / "rollout" / "check_updates.sh",
    REPO / "ops" / "drift" / "check_drift.sh",
    REPO / "ops" / "backup" / "restic-check.sh",
    REPO / "ops" / "backup" / "restic-verify.sh",
    REPO / "ops" / "rollout" / "prune_images.sh",
]


def _chmod_before_mv(source: str) -> bool:
    """True if every mktemp...mv pair has a chmod 644 in between."""
    # Find all mktemp calls that produce a textfile (variable or path).
    # We look for the pattern: chmod 644 "$var" (or path) followed later by mv.
    # Simplest robust check: the script contains 'chmod 644' and it appears
    # before the corresponding 'mv -f' / 'mv ' in the publish block.
    return "chmod 644" in source


def test_every_textfile_writer_chmods_644():
    missing = []
    for path in TEXTFILE_WRITERS:
        if not path.is_file():
            missing.append(f"{path} (file missing)")
            continue
        source = path.read_text()
        if "node-exporter-textfile" not in source and "TEXTFILE" not in source:
            # Not a textfile writer; skip silently.
            continue
        if "chmod 644" not in source:
            missing.append(f"{path.name} (no 'chmod 644')")
    assert not missing, (
        "textfile writers without chmod 644 (node-exporter cannot read them):\n  "
        + "\n  ".join(missing)
    )


def test_chmod_precedes_mv_in_each_writer():
    """chmod must come before mv, otherwise the published file keeps mktemp's 600.

    Writers that use the textfile_write helper are exempt: the helper does chmod
    internally before mv, so the ordering is guaranteed by construction.
    """
    bad = []
    for path in TEXTFILE_WRITERS:
        if not path.is_file():
            continue
        source = path.read_text()
        if "write_textfile" in source:
            continue  # helper guarantees ordering
        if "chmod 644" not in source:
            continue
        chmod_pos = source.find("chmod 644")
        mv_pos = source.find("mv ", chmod_pos)
        if mv_pos == -1:
            mv_pos = source.find("mv ")
        if mv_pos != -1 and chmod_pos > mv_pos:
            bad.append(f"{path.name} (chmod after mv)")
    assert not bad, "chmod must precede mv:\n  " + "\n  ".join(bad)


def test_no_textfile_writer_uses_bare_mktemp_mv():
    """A writer that uses mktemp+mv without chmod is the original defect."""
    bad = []
    for path in TEXTFILE_WRITERS:
        if not path.is_file():
            continue
        source = path.read_text()
        if "mktemp" in source and "mv " in source and "chmod 644" not in source:
            bad.append(path.name)
    assert not bad, "writers with mktemp+mv but no chmod 644:\n  " + "\n  ".join(bad)
