"""Runner I/O tests: fleet resolution, env parsing and token lookup.

These cover the parts that decide *which* bot gets checked with *which* token —
a wrong fleet or a mis-parsed env file would silently narrow the check, which is
how coverage gaps appear in the first place.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import webhook_check as runner

FLEET_FILE = """\
# comment line
FLEET="alpha:9001 beta:9002"
WEBHOOK_DOMAIN="example.test"
WEBHOOK_IP="198.51.100.4"
"""


def write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body)
    return path


def test_load_contract_from_explicit_file(tmp_path):
    path = write(tmp_path, "fleet.env", FLEET_FILE)
    contract = runner.load_contract(str(path))
    assert [b.name for b in contract.fleet] == ["alpha", "beta"]
    assert [b.port for b in contract.fleet] == [9001, 9002]
    assert contract.domain == "example.test"
    assert contract.ip == "198.51.100.4"
    assert contract.source == str(path)


def test_load_contract_falls_back_to_builtin_when_file_missing(tmp_path):
    """A missing or unreadable fleet.env must widen to the builtin list, not crash."""
    contract = runner.load_contract(str(write(tmp_path, "absent.env", "")))
    assert len(contract.fleet) == 9
    assert contract.fleet[0].name == "bookingbot"
    assert contract.domain == "ninelegsbots.duckdns.org"


def test_unquoted_values_and_blank_lines_are_tolerated(tmp_path):
    path = write(tmp_path, "fleet.env", "FLEET=gamma:9003\n\nWEBHOOK_DOMAIN=other.test\n")
    contract = runner.load_contract(str(path))
    assert [b.name for b in contract.fleet] == ["gamma"]
    assert contract.domain == "other.test"


def test_read_token_and_secret_from_env_file(tmp_path, monkeypatch):
    write(
        tmp_path,
        "support.env",
        'TELEGRAM_BOT_TOKEN="123:ABC"\nTELEGRAM_WEBHOOK_SECRET="s3cr3t"\nUNRELATED=x\n',
    )
    monkeypatch.setattr(runner, "ENV_ROOT", tmp_path)
    assert runner.read_token("support") == "123:ABC"
    assert runner.read_secret("support") == "s3cr3t"


def test_read_token_missing_file_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ENV_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        runner.read_token("ghost")


def test_read_token_missing_key_fails_loudly(tmp_path, monkeypatch):
    write(tmp_path, "store.env", "REDIS_URL=redis://x\n")
    monkeypatch.setattr(runner, "ENV_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        runner.read_token("store")


def test_read_secret_absent_is_empty_not_an_error(tmp_path, monkeypatch):
    write(tmp_path, "store.env", "TELEGRAM_BOT_TOKEN=1:AAA\n")
    monkeypatch.setattr(runner, "ENV_ROOT", tmp_path)
    assert runner.read_secret("store") == ""


def test_pending_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STATE_ROOT", tmp_path / "state")
    assert runner.read_pending("leadgen") is None
    runner.write_pending("leadgen", 3)
    assert runner.read_pending("leadgen") == 3


def test_log_writes_line(tmp_path):
    path = tmp_path / "check.log"
    log = runner.Log(path, echo=False)
    log("hello")
    assert "hello" in path.read_text()


def test_bot_selection_by_name(tmp_path):
    path = write(tmp_path, "fleet.env", FLEET_FILE)
    contract = runner.load_contract(str(path))
    selected = [b for b in contract.fleet if b.name == "beta"]
    assert [b.name for b in selected] == ["beta"]


def test_alert_is_posted_with_a_future_ends_at(tmp_path, monkeypatch):
    """A bare POST would let Alertmanager expire the alert in 5 minutes."""
    posted: list[list[dict]] = []
    monkeypatch.setattr(runner, "ALERT_ROOT", tmp_path / "alerted")
    monkeypatch.setattr(runner, "_post_alerts", lambda payload: posted.append(payload) or None)
    log = runner.Log(tmp_path / "log", echo=False)

    runner.send_alert("leadgen", "critical", "C5 broken", log)

    alert = posted[0][0]
    starts = datetime.fromisoformat(alert["startsAt"])
    ends = datetime.fromisoformat(alert["endsAt"])
    assert ends > starts
    assert (ends - starts) == timedelta(hours=runner.ALERT_HOLD_H)
    assert alert["labels"]["alertname"] == "BotkitWebhookDeliveryFailed"


def test_repeat_alert_stays_firing_but_does_not_notify(tmp_path, monkeypatch):
    posted: list[list[dict]] = []
    monkeypatch.setattr(runner, "ALERT_ROOT", tmp_path / "alerted")
    monkeypatch.setattr(runner, "_post_alerts", lambda payload: posted.append(payload) or None)
    log = runner.Log(tmp_path / "log", echo=False)

    runner.send_alert("leadgen", "critical", "first", log)
    runner.send_alert("leadgen", "critical", "second", log)

    assert len(posted) == 2, "the alert must be re-posted so it keeps firing"
    assert "notification throttled" in (tmp_path / "log").read_text()


def test_resolve_closes_alert_and_clears_marker(tmp_path, monkeypatch):
    posted: list[list[dict]] = []
    monkeypatch.setattr(runner, "ALERT_ROOT", tmp_path / "alerted")
    monkeypatch.setattr(runner, "_post_alerts", lambda payload: posted.append(payload) or None)
    log = runner.Log(tmp_path / "log", echo=False)

    runner.send_alert("leadgen", "critical", "broken", log)
    runner.resolve_alert("leadgen", log)

    assert len(posted) == 2
    assert posted[1][0]["endsAt"] == posted[1][0]["startsAt"], "resolve must expire the alert immediately"
    assert not (tmp_path / "alerted" / "leadgen").exists()


def test_resolve_without_marker_does_nothing(tmp_path, monkeypatch):
    posted: list[list[dict]] = []
    monkeypatch.setattr(runner, "ALERT_ROOT", tmp_path / "alerted")
    monkeypatch.setattr(runner, "_post_alerts", lambda payload: posted.append(payload) or None)
    runner.resolve_alert("quiet", runner.Log(tmp_path / "log", echo=False))
    assert posted == []


def test_alert_send_failure_leaves_no_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ALERT_ROOT", tmp_path / "alerted")
    monkeypatch.setattr(runner, "_post_alerts", lambda payload: "connection refused")
    log = runner.Log(tmp_path / "log", echo=False)

    runner.send_alert("leadgen", "critical", "broken", log)

    assert "ALERT send FAILED" in (tmp_path / "log").read_text()
    assert not (tmp_path / "alerted" / "leadgen").exists(), "must retry on the next run"


def test_warning_never_raises_an_alert(tmp_path, monkeypatch):
    posted: list[list[dict]] = []
    monkeypatch.setattr(runner, "ALERT_ROOT", tmp_path / "alerted")
    monkeypatch.setattr(runner, "_post_alerts", lambda payload: posted.append(payload) or None)
    runner.send_alert("leadgen", "warning", "C6 sustained pending", runner.Log(tmp_path / "log", echo=False))
    assert posted == []
