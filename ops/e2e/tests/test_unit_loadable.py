"""The unit must be loadable by systemd, not merely well-formed text.

A live sandbox check on the E2E host found the unit could not start at all:
`InaccessiblePaths=/home /root /srv /etc/botkit` makes systemd refuse to load
the unit whenever a listed path does not exist, and /etc/botkit does not exist
on the host that runs this. The unit exited 226 and no E2E run ever happened.

The suite was green throughout, because every assertion read the file as text.
Reading text proves the directive is spelled correctly, not that systemd
accepts it - the same gap that let a timer point at a stale copy while the fix
sat in the repository.

These tests therefore assert the two things that actually decide whether the
unit works: every path in a path-taking directive is allowed to be absent
("-/ prefix"), and the service section is internally consistent. A live
`systemd-run` check on the host is in scripts/sandbox-check.sh.
"""

import pathlib
import re

E2E_DIR = pathlib.Path(__file__).resolve().parent.parent
UNIT = (E2E_DIR / "systemd" / "botkit-e2e.service").read_text()

# Directives whose list entries name filesystem paths that may legitimately not
# exist on the E2E host.
PATH_DIRECTIVES = ("InaccessiblePaths", "ReadWritePaths", "ReadOnlyPaths", "BindPaths", "TemporaryFileSystem")


def _values(key: str) -> list[str]:
    return re.findall(rf"^{key}=(.*)$", UNIT, re.MULTILINE)


def test_unit_loads_by_systemd():
    """Positive control: if this cannot even read the file, every other test here is noise."""
    assert UNIT.strip(), "botkit-e2e.service is empty"
    assert "[Service]" in UNIT, "no [Service] section"


def test_inaccessible_paths_tolerate_missing_directories():
    """The defect found on the host: a missing path made the unit unloadable.

    systemd treats an absolute path without the "-" prefix as mandatory and
    fails the whole unit with exit 226. /etc/botkit is not on the E2E host.
    """
    values = _values("InaccessiblePaths")
    assert values, "InaccessiblePaths is required; it is the key isolation directive"
    entries = values[0].split()
    assert entries, "InaccessiblePaths is empty, which would isolate nothing"
    without_dash = [e for e in entries if not e.startswith(("-", "+", "!", "~"))]
    assert not without_dash, (
        "these paths are mandatory for systemd and the unit will not start if any is "
        f"missing on the E2E host: {without_dash}. Use the '-' prefix."
    )


def test_every_entry_is_absolute():
    for key in PATH_DIRECTIVES:
        for value in _values(key):
            for entry in value.split():
                path = entry.lstrip("-+!~")
                assert path.startswith("/"), f"{key}: {entry!r} is not an absolute path"


def test_readwritepaths_covers_exactly_session_and_status():
    """A writable path is a writable path; extra entries are extra exposure."""
    values = _values("ReadWritePaths")
    assert values, "ReadWritePaths is required"
    entries = sorted(e.lstrip("-+!~") for e in values[0].split())
    assert entries == ["/var/lib/botkit-e2e/session", "/var/lib/botkit-e2e/status"], entries


def test_session_and_status_paths_are_under_the_state_dir():
    values = _values("ReadWritePaths")
    for value in values:
        for entry in value.split():
            path = entry.lstrip("-+!~")
            assert path.startswith("/var/lib/botkit-e2e/"), f"{path} is outside /var/lib/botkit-e2e"


def test_boolean_directives_carry_an_explicit_value():
    """`-p MemoryDenyWriteExecute` is "Not an assignment"; `-p MemoryDenyWriteExecute=true` works.

    Only matters for tooling that passes directives on a command line, but the
    same ambiguity is what made the first sandbox check report nonsense.
    """
    booleans = re.findall(r"^([A-Z][A-Za-z]+)$", UNIT, re.MULTILINE)
    assert not booleans, f"these directives have no =value: {booleans}"


def test_no_directive_references_a_root_owned_copy():
    """The 26.09 incident: the unit pointed at /root while the fix was in the repo."""
    offenders = [
        line
        for line in UNIT.splitlines()
        if line.split("=", 1)[0] in {"ExecStart", "WorkingDirectory", "EnvironmentFile"} and "/root/" in line
    ]
    assert not offenders, f"unit references a root-owned copy: {offenders}"
