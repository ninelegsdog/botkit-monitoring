from pathlib import Path

import pytest

from e2e.config import load_bots, load_scenarios, load_settings


def test_load_settings(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "abc")
    monkeypatch.delenv("TG_PHONE", raising=False)
    s = load_settings()
    assert s.api_id == 123 and s.api_hash == "abc"


def test_phone_is_optional_without_tg_phone(monkeypatch):
    """W1: the E2E host must not have to keep a phone number.

    The gate is a raised error, not a default: a Settings that silently accepts
    an empty phone would let an unauthorized session reach the first scenario
    step and time out there, which reads as "the bot is broken".
    """
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "abc")
    monkeypatch.delenv("TG_PHONE", raising=False)
    assert load_settings().phone == ""


def test_load_settings_reads_new_paths(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    monkeypatch.setenv("E2E_BOTS_FILE", "/etc/botkit-e2e/bots.yml")
    monkeypatch.setenv("E2E_STATUS_DIR", "/var/lib/botkit-e2e/status")
    monkeypatch.setenv("E2E_SESSION_DIR", "/var/lib/botkit-e2e/session")
    monkeypatch.setenv("E2E_SESSION_NAME", "runner")
    monkeypatch.setenv("E2E_ALERT_URL", "http://127.0.0.1:9093/api/v2/alerts")
    s = load_settings()
    assert s.bots_file == Path("/etc/botkit-e2e/bots.yml")
    assert s.status_dir == Path("/var/lib/botkit-e2e/status")
    assert s.session_path == Path("/var/lib/botkit-e2e/session/runner.session")
    assert s.alert_url.endswith("/api/v2/alerts")


def test_session_path_is_built_from_directory_and_name(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    s = load_settings()
    assert s.session_path.name == f"{s.session_name}.session"
    assert s.session_path.parent == s.session_dir


def test_timeout_default_is_30(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    monkeypatch.delenv("E2E_TIMEOUT", raising=False)
    assert load_settings().timeout == 30


def test_timeout_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    monkeypatch.setenv("E2E_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="E2E_TIMEOUT"):
        load_settings()


def test_device_labels_are_configurable(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    monkeypatch.setenv("E2E_DEVICE_MODEL", "lab-box")
    monkeypatch.setenv("E2E_SYSTEM_VERSION", "Debian")
    monkeypatch.setenv("E2E_APP_VERSION", "botkit-e2e/9.9")
    s = load_settings()
    assert (s.device_model, s.system_version, s.app_version) == ("lab-box", "Debian", "botkit-e2e/9.9")


def test_load_scenarios(tmp_path):
    p = tmp_path / "sc.yaml"
    p.write_text("bookingbot:\n  steps:\n    - send: /start\n      expect: Привет\n")
    sc = load_scenarios(p)
    assert sc["bookingbot"].steps[0].expect == "Привет"


def test_load_bots_strips_at_sign(tmp_path):
    # Quoted: a bare @ is a YAML reserved indicator, so the unquoted form is a
    # parse error rather than a string. Resolved output is written quoted too.
    p = tmp_path / "bots.yml"
    p.write_text('botkit-bookingbot: "@bookingbot_test_bot"\nbotkit-support: "support_test_bot"\n')
    assert load_bots(p) == {
        "botkit-bookingbot": "bookingbot_test_bot",
        "botkit-support": "support_test_bot",
    }


def test_load_bots_rejects_empty_username(tmp_path):
    """An empty username must fail loudly here, not at the first send_message."""
    p = tmp_path / "bots.yml"
    p.write_text("botkit-bookingbot: bookingbot_test_bot\nbotkit-support: \n")
    with pytest.raises(ValueError, match="botkit-support"):
        load_bots(p)
