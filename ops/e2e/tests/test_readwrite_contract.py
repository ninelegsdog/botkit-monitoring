"""Contract: every `ProtectSystem=strict` unit must be allowed to write what its script writes.

The footgun: on 06.10 drift metrics were added to `check_drift.sh` (it began writing
`/var/lib/node-exporter-textfile/botkit_drift.prom`) without adding that path to
`botkit-drift.service`'s `ReadWritePaths`. `ProtectSystem=strict` makes the whole
filesystem read-only except the listed paths, so every run died with

    mktemp: Read-only file system (os error 30)

~96 times a day for five days. The unit sat in `systemctl --failed` and the metric it
publishes stayed frozen at its last value, so dashboards read green while the check
that produces them was dead. There was no alert on the metric's staleness, so nothing
fired.

`test_image_pin_contract.py` already asserts this invariant for the rollout unit - and its
docstring describes a near-identical incident on 03.10. It hardcodes a single `UNIT`, so
the other six isolated units were uncovered. This module closes the *class*:

- discover every unit carrying `ProtectSystem=strict` from the repo tree;
- for each, find the script its `ExecStart` runs and the textfile/other absolute paths
  that script writes;
- assert each such path is granted by `ReadWritePaths` (or is otherwise writable).

The pairs are derived from files, not from a list in this test, so adding an eighth
isolated unit is covered the day it lands.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SYSTEMD_DIRS = ("ops",)
# Files whose absolute write targets we can reliably detect from source.
WRITE_TARGET_HINTS = (
    re.compile(r"/var/lib/node-exporter-textfile"),
    re.compile(r"/var/backups/[A-Za-z0-9._/-]+"),
    re.compile(r"/var/log/[A-Za-z0-9._/-]+"),
    re.compile(r"/var/lib/botkit-[A-Za-z0-9._/-]+"),
)

UNIT_GLOB = "**/systemd/*.service"


def _units() -> list[pathlib.Path]:
    units = []
    for base in SYSTEMD_DIRS:
        units.extend(sorted((REPO / base).glob(UNIT_GLOB)))
    return units


def _protected_systemd_units() -> list[pathlib.Path]:
    """Units that pin the filesystem with ProtectSystem=strict."""
    out = []
    for unit in _units():
        text = unit.read_text(encoding="utf-8")
        if re.search(r"^ProtectSystem=strict\b", text, re.MULTILINE):
            out.append(unit)
    return out


def _unit_paths(unit: pathlib.Path, key: str) -> set[str]:
    """Every path granted by a `Key=a b c` line (values, no key, no '-' prefixes)."""
    found: set[str] = set()
    for line in unit.read_text(encoding="utf-8").splitlines():
        if line.startswith(key):
            for part in line[len(key) :].split():
                found.add(part.lstrip("-"))  # strip the '-' that tolerates a missing path
    return found


def _execstart_script(unit: pathlib.Path) -> pathlib.Path | None:
    text = unit.read_text(encoding="utf-8")
    m = re.search(r"^ExecStart=.*\s(/home/\S+)", text, re.MULTILINE)
    if m:
        # Map /home/deploy/<repo>/<path> back to a repo file.
        rel = m.group(1).replace("/home/deploy/botkit-monitoring/", "")
        cand = REPO / rel
        if cand.is_file():
            return cand
    # Some units run a copy under /opt or /root, or a python module. Fall back to the
    # script whose name matches the unit, so those units are still covered - botkit-
    # webhook-check.service is exactly this shape and is the unit that already carries
    # the correct textfile grant, so a regression there would be silent without this.
    stem = unit.name[: -len(".service")].removeprefix("botkit-")
    for pattern in (f"**/{stem}.py", f"**/{stem}.sh", f"**/{stem.replace('-', '_')}.py"):
        hits = sorted((REPO / "ops").glob(pattern))
        if hits:
            return hits[0]
    return None


def _write_targets(script: pathlib.Path) -> set[str]:
    """Absolute directories the script appears to write into (from literals in source)."""
    text = script.read_text(encoding="utf-8", errors="replace")
    targets: set[str] = set()
    for hint in WRITE_TARGET_HINTS:
        for m in hint.finditer(text):
            # Keep the directory portion (strip any trailing filename-ish segment).
            hit = m.group(0).rstrip("/")
            # For /var/log/x.log keep the parent /var/log.
            if hit.startswith("/var/log/"):
                hit = "/var/log" if hit.count("/") == 2 else hit.rsplit("/", 1)[0]
            else:
                hit = hit.rsplit("/", 1)[0] if hit.count("/") > 3 else hit
            targets.add(hit)
    return targets


def test_repository_has_isolated_units() -> None:
    """Guard: if this finds none the module silently stops protecting anything."""
    units = _protected_systemd_units()
    assert units, "no ProtectSystem=strict units found - the scan is misconfigured"
    # Regression anchor: this is the unit that broke on 06.10.
    names = {u.name for u in units}
    assert "botkit-drift.service" in names, (
        "botkit-drift.service must remain in scope for this class of test"
    )


@pytest.mark.parametrize("unit", _protected_systemd_units(), ids=lambda u: u.name)
def test_unit_may_write_every_path_its_script_writes(unit: pathlib.Path) -> None:
    script = _execstart_script(unit)
    if script is None:
        pytest.skip("ExecStart does not point at a repo script (nothing to cross-check)")

    targets = _write_targets(script)
    if not targets:
        pytest.skip("script writes no detected absolute targets")

    granted = _unit_paths(unit, "ReadWritePaths=")
    # Also accept anything already writable by virtue of a broader grant parent.
    def allowed(path: str) -> bool:
        if path in granted:
            return True
        return any(path.startswith(g.rstrip("/") + "/") for g in granted)

    missing = sorted(t for t in targets if not allowed(t))
    assert not missing, (
        f"{unit.name} runs {script.name}, which writes {sorted(targets)}, but ReadWritePaths "
        f"grants only {sorted(granted)}. ProtectSystem=strict will make the missing paths "
        f"read-only and the unit will fail at write time (EROFS) - the drift check did exactly "
        f"this for 5 days. Add them to ReadWritePaths (with the '-' prefix if the path may be "
        f"absent)."
    )
