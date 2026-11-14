"""Contract: alert rules must describe the failure they claim to describe.

Two rules on this branch were written from the annotation backwards:

- `BotkitRolloutCheckFailed` used `botkit_rollout_check_state == 0`. That metric is the
  poller's exit code, so 0 means *every bot rolled out*. The rule fired on a healthy run
  within five minutes of being loaded on prod.
- `HostSwapHigh` alerted on swap *occupancy*. Canon documents why it does not: Linux keeps
  cold pages in swap and never returns them, so the rule sat at 85.47% with 2 GiB of RAM
  available — a permanent warning that teaches people to ignore alerts. Real pressure is
  PSI stall, which `HostMemoryPressureHigh` already covers.

A rule that fires when nothing is wrong is worse than no rule, because it costs attention
and hides the next real alert. These tests pin the two shapes: an exit-code metric is
non-zero on failure, and every alert carries a runbook and a duration.
"""

from __future__ import annotations

import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]
ALERTS = REPO / "prometheus" / "alerts.yml"


def _rules() -> list[dict]:
    doc = yaml.safe_load(ALERTS.read_text())
    out = []
    for group in doc.get("groups", []):
        out.extend(group.get("rules", []))
    return [r for r in out if "alert" in r]


def test_every_alert_has_runbook_and_duration() -> None:
    bad = []
    for rule in _rules():
        name = rule["alert"]
        annotations = rule.get("annotations") or {}
        if not annotations.get("runbook_url"):
            bad.append(f"{name}: no runbook_url")
        if "for" not in rule:
            bad.append(f"{name}: no for:")
    assert not bad, "\n".join(bad)


def test_exit_code_metrics_are_not_alerted_on_zero() -> None:
    """`*_state` is a process exit code: 0 is success, non-zero is the failure."""
    bad = []
    for rule in _rules():
        expr = str(rule.get("expr", ""))
        if "_state" in expr and re.search(r"==\s*0\b", expr):
            bad.append(f"{rule['alert']}: {expr.strip()}")
    assert not bad, (
        "alert fires on a successful run (exit code 0 means success):\n  "
        + "\n  ".join(bad)
    )


def test_stale_alerts_detect_a_missing_metric_or_a_stale_one() -> None:
    """A *Stale alert catches a poller that stopped: absent metric, or age past a bound."""
    bad = []
    for rule in _rules():
        if not rule["alert"].endswith("Stale"):
            continue
        expr = str(rule.get("expr", ""))
        # Two legitimate shapes: the metric is missing (poller never wrote it), or the
        # metric reports an age that crossed a bound (the poller ran, the work did not).
        if "absent(" in expr or "_age_seconds" in expr or "_last_run_seconds" in expr:
            continue
        bad.append(f"{rule['alert']}: {expr.strip()}")
    assert not bad, "Stale alerts without absent() or an age bound:\n  " + "\n  ".join(bad)


def test_swap_occupancy_is_not_alerted() -> None:
    """Canon: swap share is not a failure signal; PSI stall is."""
    names = {r["alert"] for r in _rules()}
    assert "HostSwapHigh" not in names
    assert "HostMemoryPressureHigh" in names
