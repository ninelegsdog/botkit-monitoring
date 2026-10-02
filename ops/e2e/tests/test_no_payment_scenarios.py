"""D7: forbid payment scenarios in the E2E plan, and prove the rule is live.

The risk is a real Telegram payment, not a test artefact: a scenario that taps
a pay button would move money and could be triggered by the timer. The spec
named tests/test_deposit_e2e.py in botkit-bookingbot as the source of that
risk, but that file is a container database test, not a Telegram scenario - so
the named source was wrong and the real one, future scenarios, was unguarded.

A comment in scenarios.yml would be a request to reviewers. This is a gate.
"""

import pathlib

import pytest
import yaml

E2E_DIR = pathlib.Path(__file__).resolve().parent.parent

# Substrings that indicate money movement. Matched case-insensitively.
PAYMENT_MARKERS = (
    "оплат",
    "pay",
    "оплатить",
    "stars",
    "invoice",
    "счёт",
    "счет",
    "юkassa",
    "yookassa",
    "перевести",
    "перевод",
)


@pytest.fixture(scope="module")
def scenarios() -> dict:
    data = yaml.safe_load((E2E_DIR / "scenarios.yml").read_text()) or {}
    assert data, "scenarios.yml is empty; the runner would report nothing and exit 0"
    return data


def test_no_scenario_sends_a_payment_command(scenarios):
    offenders = []
    for bot, cfg in scenarios.items():
        for i, step in enumerate(cfg.get("steps", []), start=1):
            low = str(step.get("send", "")).lower()
            hit = [m for m in PAYMENT_MARKERS if m in low]
            if hit:
                offenders.append(f"{bot} step {i}: send={step.get('send')!r} matches {hit}")
    assert not offenders, "payment commands are forbidden in E2E scenarios:\n" + "\n".join(offenders)


def test_no_scenario_expects_a_payment_prompt(scenarios):
    offenders = []
    for bot, cfg in scenarios.items():
        for i, step in enumerate(cfg.get("steps", []), start=1):
            low = str(step.get("expect", "")).lower()
            hit = [m for m in PAYMENT_MARKERS if m in low]
            if hit:
                offenders.append(f"{bot} step {i}: expect={step.get('expect')!r} matches {hit}")
    assert not offenders, "payment prompts must not be expected in E2E scenarios:\n" + "\n".join(offenders)


def test_every_step_has_send_and_expect(scenarios):
    """A step missing expect would compare against nothing and pass."""
    bad = []
    for bot, cfg in scenarios.items():
        for i, step in enumerate(cfg.get("steps", []), start=1):
            if not str(step.get("send", "")).strip():
                bad.append(f"{bot} step {i}: empty send")
            if not str(step.get("expect", "")).strip():
                bad.append(f"{bot} step {i}: empty expect")
    assert not bad, "steps need both send and expect:\n" + "\n".join(bad)


def test_the_gate_actually_catches_a_payment_step(scenarios):
    """Prove the marker list still matches: a rule that matches nothing is a green light.

    If PAYMENT_MARKERS ever stops matching Telegram payment wording, the two
    tests above would pass on a scenario that pays. This test fails first.
    """
    sample = "Оплатить подписку"
    assert any(m in sample.lower() for m in PAYMENT_MARKERS)
