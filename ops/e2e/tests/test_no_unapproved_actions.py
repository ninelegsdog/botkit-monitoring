"""Inline `press` steps are limited to a read-only allowlist, and the rule is live.

The E2E account is an admin in every bot, so a scenario that pressed an arbitrary
callback_data would run that handler with admin rights against production: add to a
cart, take a ticket, subscribe, delete. A `press` step therefore only names a button
on this allowlist, and every entry is a handler that answers with a read-only view.

This is a gate, not a comment: a new callback_data slipping into scenarios.yml fails
CI, and the mutation test below proves the check still bites.
"""

import pathlib

import pytest
import yaml

E2E_DIR = pathlib.Path(__file__).resolve().parent.parent

# Inline callbacks that answer read-only and never write. Verified against the bot
# handlers: `book:my` edits "Нет активных записей." for an account with no bookings;
# `mem:my` edits "У вас нет активной подписки." for one with no subscription. Empty
# state is deterministic for the E2E test account (bookings=0, subscriptions=0).
ALLOWED_PRESS = frozenset(
    {
        "book:my",
        "mem:my",
    }
)


@pytest.fixture(scope="module")
def scenarios() -> dict:
    data = yaml.safe_load((E2E_DIR / "scenarios.yml").read_text()) or {}
    assert data, "scenarios.yml is empty; the runner would report nothing and exit 0"
    return data


def _unapproved(sc: dict) -> list[str]:
    """Offenders: press steps whose callback_data is not on the allowlist."""
    out = []
    for bot, cfg in sc.items():
        for i, step in enumerate(cfg.get("steps", []), start=1):
            press = str(step.get("press", "")).strip()
            if press and press not in ALLOWED_PRESS:
                out.append(f"{bot} step {i}: press={press!r} not in ALLOWED_PRESS")
    return out


def test_every_press_is_on_the_allowlist(scenarios):
    assert not _unapproved(scenarios), (
        "only read-only inline buttons may be pressed:\n" + "\n".join(_unapproved(scenarios))
    )


def test_the_gate_actually_catches_an_unapproved_press(scenarios):
    """Prove the allowlist still bites: a rule that matches nothing is a green light."""
    bad = {"b": {"steps": [{"press": "mem:subscribe:1", "expect": "x"}]}}
    assert _unapproved(bad), "an unapproved press (a subscribe button) must be flagged"
    good = {"b": {"steps": [{"press": "mem:my", "expect": "x"}]}}
    assert not _unapproved(good), "an allowlisted press must pass"
