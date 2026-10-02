"""Every Settings attribute the runner uses must exist.

This gate exists because of a real miss. W1 renamed `Settings.session_file` to
`Settings.session_path`, and `first_login.py` kept reading the old name. The
whole suite stayed green - the broken module was simply never imported - and
the failure would have surfaced as an AttributeError during the first login,
i.e. at the one moment when a person was waiting on the output.

An import check would not have caught it either: `settings.session_file` is an
attribute access, which only fails at runtime. So this test walks the AST of
every module in the package, collects reads of a variable named `settings` (or
`self.settings`), and asserts each name exists on Settings - as a dataclass
field or as a property.

The receiver name is matched literally on purpose. Widening it to any short
variable name produced a false positive on `s.expect` (a Scenario step), which
is the same failure mode in reverse: a gate that cries wolf gets disabled.
"""

import ast
import pathlib

from e2e.config import Settings

E2E_DIR = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = E2E_DIR / "e2e"

RECEIVERS = {"settings", "self.settings", "self._settings"}


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _settings_attributes() -> set[str]:
    """Fields plus properties: session_path is a property, not a dataclass field."""
    names = set(Settings.__dataclass_fields__)
    names |= {n for n, v in vars(Settings).items() if isinstance(v, property)}
    return names


def _settings_reads(path: pathlib.Path) -> set[str]:
    """Names read off a variable that is a Settings instance."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        if ast.unparse(node.value) in RECEIVERS:
            found.add(node.attr)
    return found


def test_every_settings_read_resolves_to_an_attribute():
    """The gate. Removing a field while a caller still reads it must fail here."""
    known = _settings_attributes()
    missing = [f"{p.name}:{n}" for p in _modules() for n in sorted(_settings_reads(p)) if n not in known]
    assert not missing, f"Settings attributes read but absent (stale caller): {', '.join(missing)}"


def test_the_reader_finds_the_known_settings_uses():
    """Self-check: the AST walk must see real reads, or the gate above is empty.

    If a refactor renames the receiver, this fails before the gate above starts
    passing for the wrong reason.
    """
    reads = _settings_reads(PACKAGE / "run_e2e.py")
    assert {"alert_url", "status_dir", "bots_file", "timeout"} <= reads, (
        f"AST walk lost track of Settings reads, found only {sorted(reads)}"
    )


def test_first_login_and_client_share_the_session_field():
    """The pair that actually broke: both must read the same session attribute."""
    for name in ("first_login.py", "client.py"):
        reads = _settings_reads(PACKAGE / name)
        assert "session_path" in reads, f"{name} must read settings.session_path, found {sorted(reads)}"
        assert "session_file" not in reads, f"{name} still reads the removed settings.session_file"


def test_every_module_parses():
    """A syntax error in a module no test imports is otherwise invisible."""
    for path in _modules():
        ast.parse(path.read_text(), filename=str(path))
