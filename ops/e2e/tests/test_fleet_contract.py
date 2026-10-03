"""S1 contract: fleet.env is the only place a monitoring address is written.

The plan required a gate that fails when any port map is removed. This goes
further, because the first version of S1 changed seven files rather than the six
the plan counted, and one of them was broken by my own edit in a way no
text assertion would have caught: restic-check.sh appends /api/v2/alerts to the
URL it is given, so pointing it at the full alerts URL produced
.../api/v2/alerts/api/v2/alerts, and `curl -sf` turned that 404 into silence.

Three failure modes, three tests:

1. a port map deleted from fleet.env          -> required keys
2. a script reintroducing a hardcoded address  -> no literals outside fleet.env
3. a script building a URL by concatenation   -> no /api/ path literals in scripts

The self-check at the end exists because (2) and (3) pass trivially if the file
scan finds nothing to look at.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

# This file lives in ops/e2e/tests/: parents[3] is the repository root.
REPO = pathlib.Path(__file__).resolve().parents[3]
OPS = REPO / "ops"
FLEET_ENV = OPS / "lib" / "fleet.env"
FLEET_SH = OPS / "lib" / "fleet.sh"

REQUIRED = {
    "FLEET",
    "WEBHOOK_DOMAIN",
    "WEBHOOK_IP",
    "BASE_URL",
    "PROMETHEUS_URL",
    "ALERTMANAGER_URL",
    "ALERTMANAGER_ALERTS_URL",
    "ALERTMANAGER_ALERTS_ACTIVE_URL",
    "GRAFANA_URL",
    "LOKI_URL",
    "TEMPO_URL",
    "MINIO_URL",
    "MINIO_CONSOLE_URL",
    "OTLP_HTTP_URL",
    "OTLP_GRPC_URL",
    "WG_OBSERVE_SUBNET",
    "WG_OBSERVE_PROD_IP",
    "WG_OBSERVE_MONITOR_IP",
    "WG_OBSERVE_PORT",
}

FLEET_BOTS = {
    "bookingbot",
    "leadgen",
    "store",
    "support",
    "membership",
    "pricesentry",
    "docuflow",
    "delivery",
    "reminder",
}

# Operational scripts that must not name a monitoring address themselves.
GUARDED = [
    "ops/backup/check_backups.sh",
    "ops/backup/restic-check.sh",
    "ops/drift/check_drift.sh",
    "ops/drift/otlp-e2e.sh",
    "ops/e2e/smoke_all.sh",
    "ops/e2e/e2e-alerting.sh",
    "ops/e2e/webhook_check.py",
    # Added 2026-10-03 with the rollout subsystem: it was the only operational script with
    # no counterpart in git, so nothing could check it. check_updates.sh polls GitHub and
    # GHCR and names no monitoring address, which is what GUARDED asserts.
    "ops/rollout/check_updates.sh",
]

# deploy_rollout.sh deliberately stays out of GUARDED. It pings each bot's own /health and
# /metrics on 127.0.0.1:<port> from its port map, and that is the bot's port, not a
# monitoring endpoint - GUARDED would flag correct code. What mattered there was the
# Alertmanager URL, which was hardcoded and is now $ALERTMANAGER_ALERTS_URL from fleet.env;
# test_deploy_rollout_takes_alerts_from_fleet_env below pins that one requirement.
ROLLOUT_NEEDS_FLEET = ["ops/rollout/deploy_rollout.sh"]

ADDRESS = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|localhost):(?:[0-9]{2,5})\b")


def _parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_pairs() -> dict[str, str]:
    return _parse_env(FLEET_ENV)


def test_fleet_env_exists_and_is_parsed():
    assert FLEET_ENV.is_file(), f"{FLEET_ENV} is the single source of truth and must exist"
    assert _env_pairs(), "fleet.env parsed as empty"


@pytest.mark.parametrize("key", sorted(REQUIRED))
def test_required_key_present_and_non_empty(key):
    """Gate 1: deleting any port map fails here, by name."""
    values = _env_pairs()
    assert key in values, f"fleet.env lost {key}"
    assert values[key], f"{key} is empty in fleet.env"


def test_loader_validates_every_required_key():
    """fleet.sh must fail at load time, otherwise a missing key becomes a silent no-op curl."""
    text = FLEET_SH.read_text()
    missing = sorted(k for k in REQUIRED if k not in text)
    assert not missing, f"fleet.sh does not check these keys: {missing}"
    assert "_required" in text, "fleet.sh has no validation loop"


def test_alerts_url_is_a_prefix_extension_of_alertmanager_url():
    """They are used for different things; a full URL appended to itself is the bug
    that produced .../api/v2/alerts/api/v2/alerts in restic-check.sh."""
    v = _env_pairs()
    assert v["ALERTMANAGER_ALERTS_URL"].startswith(v["ALERTMANAGER_URL"]), (
        "ALERTMANAGER_ALERTS_URL must extend ALERTMANAGER_URL so scripts can use either"
    )


def test_fleet_covers_exactly_nine_bots():
    v = _env_pairs()
    bots = {e.partition(":")[0] for e in v["FLEET"].split()}
    assert bots == FLEET_BOTS, f"FLEET drifted: {bots ^ FLEET_BOTS}"


@pytest.mark.parametrize("rel", GUARDED)
def test_script_does_not_hardcode_a_monitoring_address(rel):
    """Gate 2: no literal host:port outside fleet.env and its loader."""
    path = REPO / rel
    assert path.is_file(), f"{rel} listed in the guard is missing"
    offenders = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if line.startswith("#"):
            continue  # comments explain the old value on purpose
        for m in ADDRESS.finditer(raw):
            offenders.append(f"{rel}:{lineno} {m.group(0)}")
    assert not offenders, "hardcoded monitoring addresses remain:\n" + "\n".join(offenders)


@pytest.mark.parametrize("rel", [g for g in GUARDED if g.endswith(".sh")])
def test_shell_script_does_not_build_api_paths_by_hand(rel):
    """Gate 3: appending /api/v2/alerts to a URL that already has it is silent under curl -sf."""
    text = (REPO / rel).read_text()
    bad = [f"{rel}: /api/v2/alerts" for _ in [0] if "/api/v2/alerts" in text]
    assert not bad, (
        f"{bad} - take $ALERTMANAGER_ALERTS_URL from fleet.env instead of concatenating a path"
    )


def test_shell_scripts_source_the_loader():
    for rel in [g for g in GUARDED if g.endswith(".sh")]:
        text = (REPO / rel).read_text()
        assert "lib/fleet.sh" in text, f"{rel} does not source ops/lib/fleet.sh"


def test_python_takes_the_alert_url_from_fleet_env():
    """webhook_check.py parses fleet.env for everything else; a module constant for
    the alert URL would make the destination unchangeable."""
    path = REPO / "ops/e2e/webhook_check.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in {"AM_URL", "ALERT_URL"} for t in node.targets
        ):
            msg = f"webhook_check.py line {node.lineno} assigns a module-level alert URL"
            if isinstance(node.value, ast.Constant):
                pytest.fail(msg + " - it must come from fleet.env")
    src = path.read_text()
    assert "ALERTMANAGER_ALERTS_URL" in src, "webhook_check.py never reads the alert URL"


BOT_PORT = re.compile(
    r"\b(?:bookingbot|leadgen|store|support|membership|pricesentry|docuflow|delivery|reminder):[0-9]{4}\b"
)


@pytest.mark.parametrize("rel", GUARDED)
def test_script_does_not_inline_the_fleet(rel):
    """The S1 edit removed a `BOTS="${FLEET:-bookingbot:8081 ...}"` fallback from two
    scripts. A copy of the list inline is two sources of truth that drift quietly,
    and nothing caught it until this was written as a mutation."""
    path = REPO / rel
    offenders = [
        f"{rel}:{lineno} {m.group(0)}"
        for lineno, raw in enumerate(path.read_text().splitlines(), start=1)
        for m in [BOT_PORT.search(raw)]
        if m
    ]
    assert not offenders, "the fleet is inlined instead of read from fleet.env:\n" + "\n".join(offenders)


def test_bot_port_scanner_matches_a_known_mapping():
    """Self-check for the scanner above."""
    assert BOT_PORT.search("FLEET=\"bookingbot:8081 reminder:8089\""), "scanner stopped matching"


def test_the_scanners_actually_find_known_addresses():
    """Self-check. A guard that matches nothing is a green light."""
    sample = 'curl "http://127.0.0.1:9093/api/v2/alerts"\n'
    assert ADDRESS.search(sample), "the address regex stopped matching"
    assert "127.0.0.1:9093" in sample


@pytest.mark.parametrize("rel", ROLLOUT_NEEDS_FLEET)
def test_deploy_rollout_takes_alerts_from_fleet_env(rel):
    """The one address deploy_rollout.sh must not name itself is the Alertmanager one.

    Its per-bot health and metrics URLs are legitimately literal, which is why this script
    is not in GUARDED. The alert destination is different: a hardcoded value there is what
    kept the whole rollout subsystem outside git, because the contract test failed it.
    """
    text = (REPO / rel).read_text()
    assert "lib/fleet.sh" in text, f"{rel} does not source ops/lib/fleet.sh"
    offenders = [
        f"{rel}:{i}"
        for i, line in enumerate(text.splitlines(), 1)
        if "/api/v2/alerts" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"hardcoded alert endpoint in {offenders} - take $ALERTMANAGER_ALERTS_URL from fleet.env"
    )
    assert 'AM_URL="$ALERTMANAGER_ALERTS_URL"' in text, f"{rel} does not take the URL from fleet.env"
