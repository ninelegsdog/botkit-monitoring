from e2e.config import load_scenarios, load_settings


def test_load_settings(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "abc")
    monkeypatch.setenv("TG_PHONE", "+000")
    s = load_settings()
    assert s.api_id == 123 and s.api_hash == "abc"


def test_load_scenarios(tmp_path):
    p = tmp_path / "sc.yaml"
    p.write_text("bookingbot:\n  steps:\n    - send: /start\n      expect: Привет\n")
    sc = load_scenarios(p)
    assert sc["bookingbot"].steps[0].expect == "Привет"
