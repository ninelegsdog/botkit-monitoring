from pathlib import Path

from e2e.config import load_scenarios


def test_all_nine():
    sc = load_scenarios(Path(__file__).resolve().parents[1] / "scenarios.yml")
    exp = {"botkit-bookingbot", "botkit-delivery", "botkit-docuflow", "botkit-leadgen",
           "botkit-membership", "botkit-pricesentry", "botkit-reminder", "botkit-store", "botkit-support"}
    assert exp.issubset(set(sc))
    for b, s in sc.items():
        assert s.steps, f"{b} empty"


def _step(scenarios, bot: str, *, send: str = "", press: str = ""):
    steps = scenarios[bot].steps
    for step in steps:
        if send and step.send == send:
            return step
        if press and step.press == press:
            return step
    raise AssertionError(f"{bot}: no step with send={send!r} press={press!r}")


def test_state_dependent_views_accept_both_replies():
    """The E2E account is an admin everywhere, so it can hold real state.

    Membership's mem:my edits to the empty state or to a subscription card; the reminder
    list answers the empty state or one line per reminder. Asserting only the empty branch
    turned valid work into BotNotAnswering/UnexpectedReply on every run from 12.11, and the
    page sent the reader to debug two healthy bots. `expect_any` is the documented way to
    say "both of these are correct"; this test keeps the two known state-dependent views
    from drifting back to a single branch.
    """
    scenarios = load_scenarios(Path(__file__).resolve().parents[1] / "scenarios.yml")

    subscription = _step(scenarios, "botkit-membership", press="mem:my")
    assert {"У вас нет активной подписки", "👤 Подписка:"} <= set(subscription.expect_any)

    reminders = _step(scenarios, "botkit-reminder", send="⏰ Мои напоминания")
    assert {"Нет напоминаний", " — "} <= set(reminders.expect_any)
