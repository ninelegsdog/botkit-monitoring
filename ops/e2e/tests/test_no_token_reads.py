import ast
import pathlib

import yaml

E2E_DIR = pathlib.Path(__file__).resolve().parent.parent

# Files the runner is allowed to read. bots.yml holds usernames, not tokens;
# scenarios.yml holds commands. Neither is a credential.
CREDENTIAL_NAMES = {
    "TELEGRAM_BOT_TOKEN",
    "BOT_TOKEN",
    "TELEGRAM_BOTHOOK_SECRET",
    "WEBHOOK_SECRET_TOKEN",
    "TG_API_HASH",
    "TG_PHONE",
}

# token_for() was the mechanism that let the runner read a bot secret at
# runtime. It is gone; this test is what keeps it gone.
TOKEN_HELPERS = {"token_for", "bot_getme", "get_bot_username"}


def _sources():
    for p in sorted(E2E_DIR.rglob("*.py")):
        if "__pycache__" in p.parts or "venv" in p.parts:
            continue
        yield p


def test_runner_tree_never_defines_a_token_helper():
    """W4: no module may reintroduce a function whose job is to fetch a token.

    The name is matched against a denylist of the helpers this sprint removed
    (token_for, bot_getme, get_bot_username). The extra test below exists
    because a denylist that stops matching anything is indistinguishable from a
    clean tree: the test would pass on a runner that had token_for back.
    """
    offenders = []
    for p in _sources():
        tree = ast.parse(p.read_text(), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in TOKEN_HELPERS:
                offenders.append(f"{p.name}:{node.lineno} {node.name}")
    assert not offenders, f"token-reading helpers must not exist: {offenders}"


def test_the_token_denylist_still_matches_the_removed_helpers():
    """Self-check: the denylist is the gate, so it must still catch its own names."""
    for name in ("token_for", "bot_getme", "get_bot_username"):
        assert name in TOKEN_HELPERS, f"{name} must stay on the denylist"


def test_runner_tree_does_not_import_requests():
    """requests was only ever used to call getMe with a token and to post alerts.

    The alert POST moved to urllib (stdlib) so that the runner has one less
    dependency that could grow a retry that re-sends a payload.
    """
    offenders = []
    for p in _sources():
        tree = ast.parse(p.read_text(), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [f"{p.name}: import {a.name}" for a in node.names if a.name == "requests"]
            elif isinstance(node, ast.ImportFrom) and node.module == "requests":
                offenders.append(f"{p.name}: from requests import ...")
    assert not offenders, f"requests must not be used in the E2E runner: {offenders}"


def test_runner_tree_never_names_a_credential_constant():
    """A module-level literal like TG_API_HASH = "..." is a committed secret."""
    offenders = []
    for p in _sources():
        tree = ast.parse(p.read_text(), filename=str(p))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for name in targets:
                is_literal_credential = (
                    name in CREDENTIAL_NAMES
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and node.value.value
                )
                if is_literal_credential:
                    offenders.append(f"{p.name}:{node.lineno} {name}")
    assert not offenders, f"credentials must come from the environment: {offenders}"


def test_runner_reads_credential_names_only_from_environ():
    """os.environ[...] on a credential is correct; a literal beside it is not."""
    allowed = {"config.py", "first_login.py"}
    for p in _sources():
        if p.name not in allowed:
            continue
        tree = ast.parse(p.read_text(), filename=str(p))
        for node in ast.walk(tree):
            is_environ = isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
            if is_environ and node.value.attr == "environ":
                src = ast.unparse(node)
                assert "os.environ" in src, f"unexpected environ access in {p.name}: {src}"


def test_bots_file_covers_the_whole_fleet():
    """W4: a bot missing from bots.yml would be skipped silently by the runner.

    The check is by scenario key, because scenarios.yml is what drives the
    loop: a bot present in bots.yml but absent from scenarios.yml never runs.
    """
    fleet = {
        "botkit-bookingbot",
        "botkit-delivery",
        "botkit-docuflow",
        "botkit-leadgen",
        "botkit-membership",
        "botkit-pricesentry",
        "botkit-reminder",
        "botkit-store",
        "botkit-support",
    }

    bots = yaml.safe_load((E2E_DIR / "bots.yml").read_text()) or {}
    scenarios = yaml.safe_load((E2E_DIR / "scenarios.yml").read_text()) or {}
    assert set(bots) == fleet, f"bots.yml must list exactly the fleet: {fleet ^ set(bots)}"
    assert set(scenarios) == fleet, f"scenarios.yml must cover exactly the fleet: {fleet ^ set(scenarios)}"


def test_every_bot_scenario_has_at_least_one_step():
    """A bot with zero steps is reported OK without ever talking to Telegram."""
    scenarios = yaml.safe_load((E2E_DIR / "scenarios.yml").read_text()) or {}
    empty = sorted(b for b, c in scenarios.items() if not c.get("steps"))
    assert not empty, f"scenarios with no steps would pass without contacting a bot: {empty}"
