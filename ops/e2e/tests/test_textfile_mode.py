"""Contract: every writer into node-exporter's textfile dir makes its file readable.

On 14.11 the healthcheck and rollout pollers wrote their textfiles with `mktemp` + `mv`
and no `chmod`. `mktemp` creates 0600, `mv` preserves the mode, and node-exporter runs
as an unprivileged user inside its container, so it could not read the files. The metrics
simply did not exist, and the guarding alerts are `absent(...) or ...`, which is true for
a missing metric — so a running poller looked exactly like a dead one.

The first CI version of this gate grepped for the directory path and demanded the string
`chmod 644` from every file that mentioned it. That flagged the *readers* — `host-check.sh`
validates the modes, `check_backups.sh` reads the metric, `restic-check-test.sh` points the
writer at a temp dir — because they name the same path. A gate that fails on correct code
is a gate that gets deleted.

The rule here is about behaviour: find the variables assigned to a path inside the textfile
dir, then ask whether the script writes to one of them (`>`, `>>`, `mv`). If it writes, it
must use the `write_textfile` helper or `chmod 644` explicitly. Readers are exempt.

These are contract tests: running each writer needs a live host, a textfile directory and
stubbed systemctl/docker/curl. The shape that matters is in the source.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[3]
TEXTFILE_DIR = "/var/lib/node-exporter-textfile"
HELPER = "write_textfile"

_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=", re.M)


def _publishes(source: str) -> bool:
    """True if the script writes into the textfile directory."""
    if HELPER in source:
        return True
    if TEXTFILE_DIR not in source:
        return False

    # Variables whose value points inside the textfile dir (including ${VAR:-default}).
    targets = set()
    for line in source.splitlines():
        m = _ASSIGN.match(line)
        if m and TEXTFILE_DIR in line:
            targets.add(m.group(1))
    if not targets:
        return False

    for name in targets:
        ref = rf'\$\{{?{name}\b'
        if re.search(rf'>>?\s*"?{ref}', source):
            return True
        if re.search(rf'\bmv\b[^\n]*{ref}', source):
            return True
    return False


def _violation(source: str) -> bool:
    """True if the script publishes without guaranteeing mode 644."""
    return _publishes(source) and HELPER not in source and "chmod 644" not in source


def test_gate_detects_bare_mktemp_mv():
    """The original defect: mktemp 0600, mv keeps it, no chmod."""
    source = (
        'TEXTFILE=/var/lib/node-exporter-textfile/x.prom\n'
        "tmp=$(mktemp)\n"
        'cat metrics > "$tmp"\n'
        'mv -f "$tmp" "$TEXTFILE"\n'
    )
    assert _violation(source)


def test_gate_accepts_explicit_chmod():
    source = (
        'TEXTFILE=/var/lib/node-exporter-textfile/x.prom\n'
        "tmp=$(mktemp)\n"
        'cat metrics > "$tmp"\n'
        'chmod 644 "$tmp"\n'
        'mv -f "$tmp" "$TEXTFILE"\n'
    )
    assert not _violation(source)


def test_gate_accepts_shared_helper():
    source = f'{HELPER} "$TEXTFILE" "metric 1"\n'
    assert not _violation(source)


def test_gate_ignores_reader_and_test_harness():
    """Naming the path is not writing to it."""
    reader = (
        'RUNMETRIC=/var/lib/node-exporter-textfile/botkit_backup_run.prom\n'
        '[ -r "$RUNMETRIC" ] && awk "{print $2}" "$RUNMETRIC"\n'
    )
    harness = (
        'tmpdir=$(mktemp -d)\n'
        'TEXTFILE="$tmpdir/out.prom" RUNMETRIC="$tmpdir/run.prom" bash ops/backup/restic-check.sh\n'
    )
    assert not _publishes(reader)
    assert not _publishes(harness)


def test_readers_are_not_publishers():
    for path in (
        REPO / "ops" / "lib" / "host-check.sh",
        REPO / "ops" / "backup" / "check_backups.sh",
        REPO / "ops" / "backup" / "restic-check-test.sh",
    ):
        assert not _publishes(path.read_text()), f"{path.name} only reads the textfile dir"


def test_every_repo_publisher_guarantees_644():
    bad = []
    for path in sorted((REPO / "ops").rglob("*.sh")):
        if _violation(path.read_text()):
            bad.append(str(path.relative_to(REPO)))
    assert not bad, (
        "textfile publishers without 644 (node-exporter cannot read them):\n  "
        + "\n  ".join(bad)
    )


def test_chmod_precedes_mv_in_each_hand_rolled_writer():
    """chmod must come before mv, otherwise the published file keeps mktemp's 600."""
    bad = []
    for path in sorted((REPO / "ops").rglob("*.sh")):
        source = path.read_text()
        if not _publishes(source) or HELPER in source or "chmod 644" not in source:
            continue
        start = source.find("mktemp")
        chmod_pos = source.find("chmod 644", start)
        mv_pos = source.find("mv ", start)
        if mv_pos != -1 and chmod_pos > mv_pos:
            bad.append(path.name)
    assert not bad, "chmod must precede mv:\n  " + "\n  ".join(bad)
