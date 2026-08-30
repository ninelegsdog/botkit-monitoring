from pathlib import Path

from e2e.config import load_scenarios


def test_all_nine():
    sc = load_scenarios(Path("scenarios.yml"))
    exp = {"botkit-bookingbot", "botkit-delivery", "botkit-docuflow", "botkit-leadgen",
           "botkit-membership", "botkit-pricesentry", "botkit-reminder", "botkit-store", "botkit-support"}
    assert exp.issubset(set(sc))
    for b, s in sc.items():
        assert s.steps, f"{b} empty"
