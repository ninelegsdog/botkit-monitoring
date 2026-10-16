"""Contract: the tunnel watchdog actually covers every tunnel unit.

14.10.2026: botkit-e2e-tunnel.service hit systemd's StartLimitBurst, was marked
failed, and stayed dead ~4h. `Restart=always` cannot recover from that - only an
external `reset-failed` + start can, which is what the watchdog does. These tests
keep the watchdog honest: the script must name every tunnel unit the repo carries,
the service must run the script, and the timer must be periodic. A watchdog that
silently stops covering a tunnel is indistinguishable from no watchdog until the
next outage - at which point the tower is already down.
"""

from __future__ import annotations

import os
import pathlib
import re

E2E_DIR = pathlib.Path(__file__).resolve().parent.parent
SYSTEMD = E2E_DIR / "systemd"
SCRIPT = E2E_DIR / "scripts" / "tunnel-watchdog.sh"
SERVICE = (SYSTEMD / "botkit-e2e-tunnel-watchdog.service").read_text(encoding="utf-8")
TIMER = (SYSTEMD / "botkit-e2e-tunnel-watchdog.timer").read_text(encoding="utf-8")


def _repo_tunnel_units() -> set[str]:
    return {p.name for p in SYSTEMD.glob("botkit-e2e-*tunnel.service")}


def _script_tunnel_units() -> set[str]:
    text = SCRIPT.read_text(encoding="utf-8")
    return set(re.findall(r"(botkit-e2e-[a-z0-9-]*tunnel\.service)", text))


def test_watchdog_names_every_tunnel_unit() -> None:
    repo = _repo_tunnel_units()
    assert repo, "no tunnel units found - the scan is misconfigured"
    covered = _script_tunnel_units()
    assert covered == repo, (
        f"watchdog covers {sorted(covered)} but the repo carries {sorted(repo)}; a "
        "tunnel without a watchdog entry dies silently on the next start-limit outage"
    )


def test_service_runs_the_watchdog_script() -> None:
    assert "scripts/tunnel-watchdog.sh" in SERVICE, "the service does not run the watchdog script"


def test_timer_fires_repeatedly() -> None:
    assert re.search(r"^OnUnitActiveSec=", TIMER, re.MULTILINE), "timer is not periodic"


def test_watchdog_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK), "the watchdog script must be executable (chmod +x)"
