"""Contract tests for the webhook delivery check.

Each negative fixture must fail for its own reason: a fixture that passes when the
corresponding check is deleted is a broken test, not a healthy fleet.
"""

from __future__ import annotations

import json

import pytest

from webhook_check_lib import (
    CRITICAL,
    WARNING,
    WebhookInfo,
    WebhookInfoError,
    apply_fault,
    evaluate,
    parse_webhook_info,
    worst_severity,
)

EXPECTED_URL = "https://ninelegsbots.duckdns.org/webhook/bookingbot"


def payload(**overrides) -> str:
    result = {
        "url": EXPECTED_URL,
        "has_custom_certificate": False,
        "pending_update_count": 0,
        "last_error_message": None,
        "last_error_date": None,
        "ip_address": "2.27.204.95",
    }
    result.update(overrides)
    return json.dumps({"ok": True, "result": result})


def healthy() -> WebhookInfo:
    return parse_webhook_info(payload())


def check_ids(failures) -> set[str]:
    return {f.check for f in failures}


def test_healthy_snapshot_has_no_failures():
    assert evaluate(healthy(), expected_url=EXPECTED_URL, expected_ip="2.27.204.95") == []


def test_c2_pinned_self_signed_certificate_is_critical():
    """The exact regression from 26.09.2026 must be caught by C2."""
    info = parse_webhook_info(payload(has_custom_certificate=True))
    failures = evaluate(info, expected_url=EXPECTED_URL)
    assert "C2" in check_ids(failures)
    assert worst_severity(failures) == CRITICAL


def test_c5_last_error_message_is_critical():
    info = parse_webhook_info(
        payload(last_error_message="SSL error {error:0A000086:SSL routines::certificate verify failed}")
    )
    failures = evaluate(info, expected_url=EXPECTED_URL)
    assert "C5" in check_ids(failures)
    assert any("certificate verify failed" in f.reason for f in failures)


def test_c3_url_mismatch_is_critical():
    info = parse_webhook_info(payload(url="https://ninelegsbots.duckdns.org/webhook/store"))
    assert "C3" in check_ids(evaluate(info, expected_url=EXPECTED_URL))


def test_c4_missing_secret_is_critical():
    failures = evaluate(healthy(), expected_url=EXPECTED_URL, secret_configured=False)
    assert "C4" in check_ids(failures)


def test_c6_pending_over_threshold_is_critical():
    info = parse_webhook_info(payload(pending_update_count=5))
    failures = evaluate(info, expected_url=EXPECTED_URL, prev_pending=0)
    assert "C6" in check_ids(failures)
    assert worst_severity(failures) == CRITICAL


def test_c6_pending_sustained_is_warning_not_critical():
    info = parse_webhook_info(payload(pending_update_count=2))
    failures = evaluate(info, expected_url=EXPECTED_URL, prev_pending=3)
    assert "C6" in check_ids(failures)
    assert worst_severity(failures) == WARNING


def test_c6_pending_below_threshold_and_not_sustained_is_silent():
    info = parse_webhook_info(payload(pending_update_count=2))
    assert evaluate(info, expected_url=EXPECTED_URL, prev_pending=0) == []


def test_c8_ip_mismatch_is_warning_and_skipped_in_quick_mode():
    info = parse_webhook_info(payload(ip_address="203.0.113.7"))
    failures = evaluate(info, expected_url=EXPECTED_URL, expected_ip="2.27.204.95")
    assert "C8" in check_ids(failures)
    assert worst_severity(failures) == WARNING
    assert evaluate(info, expected_url=EXPECTED_URL, expected_ip="2.27.204.95", quick=True) == []


def test_not_ok_stops_at_c1_without_speculating():
    body = json.dumps({"ok": False, "description": "Bad Request: chat not found"})
    info = parse_webhook_info(body)
    failures = evaluate(info, expected_url=EXPECTED_URL)
    assert check_ids(failures) == {"C1"}


def test_parse_rejects_non_json():
    with pytest.raises(WebhookInfoError):
        parse_webhook_info("<html>502</html>")


def test_parse_rejects_non_integer_pending():
    with pytest.raises(WebhookInfoError):
        parse_webhook_info(json.dumps({"ok": True, "result": {"pending_update_count": "many"}}))


def test_parse_accepts_missing_result_block():
    info = parse_webhook_info(json.dumps({"ok": False, "description": "Unauthorized"}))
    assert info.ok is False
    assert "Unauthorized" in info.description


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("bad_url", "C3"),
        ("custom_cert", "C2"),
        ("stale_error", "C5"),
        ("pending", "C6"),
        ("not_ok", "C1"),
        ("bad_ip", "C8"),
    ],
)
def test_every_fault_is_detected(fault, expected):
    info = apply_fault(healthy(), fault)
    assert expected in check_ids(evaluate(info, expected_url=EXPECTED_URL, expected_ip="2.27.204.95"))


def test_unknown_fault_is_rejected():
    with pytest.raises(ValueError):
        apply_fault(healthy(), "no-such-fault")


def test_fault_none_returns_identical_snapshot():
    info = healthy()
    assert apply_fault(info, "none") is info
