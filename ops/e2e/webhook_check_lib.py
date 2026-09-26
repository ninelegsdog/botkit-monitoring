"""Pure evaluation logic for the Telegram webhook delivery check.

Kept free of network and filesystem access so the contract can be unit-tested
against fixtures instead of against a live bot. The runner (``webhook_check.py``)
does I/O and delegates every verdict here.

Checks implemented here (ids match PLAN-WEBHOOK-DELIVERY.md):

  C1  getWebhookInfo answered ok
  C2  has_custom_certificate is false  -- regression guard for the 26.09.2026
      outage, where a pinned self-signed certificate made Telegram reject the
      publicly trusted certificate the proxy started serving
  C3  registered url matches the contract
  C4  secret_token is configured (see module notes on limits)
  C5  last_error_message is null
  C6  pending_update_count within budget (sustained growth -> warning)
  C8  registered ip_address matches the contract (warning)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

CRITICAL = "critical"
WARNING = "warning"

PENDING_CRITICAL = 5
FAULTS = ("none", "bad_url", "custom_cert", "stale_error", "pending", "not_ok", "bad_ip")

_MUTATIONS: dict[str, Callable[[WebhookInfo], WebhookInfo]] = {
    "bad_url": lambda i: replace(i, url="https://ninelegsbots.duckdns.org/webhook/wrong-bot"),
    "custom_cert": lambda i: replace(i, has_custom_certificate=True),
    "stale_error": lambda i: replace(
        i,
        last_error_message="SSL error {error:0A000086:SSL routines::certificate verify failed}",
        last_error_date=1,
        pending_update_count=2,
    ),
    "pending": lambda i: replace(i, pending_update_count=PENDING_CRITICAL),
    "not_ok": lambda i: replace(i, ok=False, description="Bad Request: chat not found"),
    "bad_ip": lambda i: replace(i, ip_address="203.0.113.7"),
}


@dataclass(frozen=True)
class WebhookInfo:
    """Normalised subset of the Telegram getWebhookInfo result."""

    ok: bool
    url: str
    has_custom_certificate: bool
    pending_update_count: int
    last_error_message: str | None
    last_error_date: int | None
    ip_address: str | None
    description: str = ""


@dataclass(frozen=True)
class Failure:
    check: str
    severity: str
    reason: str

    def __str__(self) -> str:
        return f"{self.check}[{self.severity}]: {self.reason}"


class WebhookInfoError(ValueError):
    """Raised when the Bot API payload cannot be interpreted."""


def parse_webhook_info(payload: str) -> WebhookInfo:
    """Parse a getWebhookInfo response body.

    Raises WebhookInfoError on non-JSON, non-object or non-integer pending count —
    a malformed payload must fail loudly rather than look healthy.
    """
    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WebhookInfoError(f"response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WebhookInfoError(f"response is not an object: {type(data).__name__}")

    result = data.get("result")
    if not isinstance(result, dict):
        return WebhookInfo(
            ok=bool(data.get("ok", False)),
            url="",
            has_custom_certificate=False,
            pending_update_count=0,
            last_error_message=data.get("description"),
            last_error_date=None,
            ip_address=None,
            description=str(data.get("description") or ""),
        )

    pending = result.get("pending_update_count", 0)
    if isinstance(pending, bool) or not isinstance(pending, int):
        raise WebhookInfoError(f"pending_update_count is not an int: {pending!r}")

    return WebhookInfo(
        ok=bool(data.get("ok", False)),
        url=str(result.get("url") or ""),
        has_custom_certificate=bool(result.get("has_custom_certificate", False)),
        pending_update_count=pending,
        last_error_message=result.get("last_error_message"),
        last_error_date=result.get("last_error_date"),
        ip_address=result.get("ip_address"),
        description=str(data.get("description") or ""),
    )


def apply_fault(info: WebhookInfo, fault: str) -> WebhookInfo:
    """Return a copy of *info* mutated to imitate a specific failure.

    Used by ``--fault`` so the negative drill exercises the real alerting path
    instead of a hand-written log line.
    """
    if fault == "none":
        return info
    if fault not in _MUTATIONS:
        raise ValueError(f"unknown fault {fault!r}, expected one of {FAULTS}")
    return _MUTATIONS[fault](info)


def evaluate(
    info: WebhookInfo,
    *,
    expected_url: str,
    expected_ip: str | None = None,
    secret_configured: bool = True,
    prev_pending: int | None = None,
    quick: bool = False,
) -> list[Failure]:
    """Turn one getWebhookInfo snapshot into a list of failures (empty = healthy)."""
    failures: list[Failure] = []

    if not info.ok:
        failures.append(Failure("C1", CRITICAL, f"getWebhookInfo not ok: {info.description or 'no description'}"))
        return failures

    if info.has_custom_certificate:
        failures.append(
            Failure(
                "C2",
                CRITICAL,
                "has_custom_certificate=true: Telegram validates the served certificate against the "
                "pinned self-signed key and rejects it ('certificate verify failed'). Re-register the "
                "webhook WITHOUT the certificate field while a public certificate is served.",
            )
        )

    if info.url != expected_url:
        failures.append(
            Failure("C3", CRITICAL, f"url mismatch: registered={info.url or '<empty>'} expected={expected_url}")
        )

    if not secret_configured:
        failures.append(
            Failure("C4", CRITICAL, "TELEGRAM_WEBHOOK_SECRET missing/empty — a re-registration would drop the secret")
        )

    if info.last_error_message:
        failures.append(
            Failure("C5", CRITICAL, f"last_error_message={info.last_error_message!r} (at {info.last_error_date})")
        )

    if info.pending_update_count >= PENDING_CRITICAL:
        failures.append(
            Failure("C6", CRITICAL, f"pending_update_count={info.pending_update_count} (>= {PENDING_CRITICAL})")
        )
    elif info.pending_update_count > 0 and prev_pending is not None and prev_pending > 0:
        failures.append(
            Failure(
                "C6",
                WARNING,
                f"pending_update_count={info.pending_update_count}, sustained since last run (prev={prev_pending})",
            )
        )

    if not quick and expected_ip is not None and info.ip_address not in (None, expected_ip):
        failures.append(
            Failure("C8", WARNING, f"ip_address mismatch: registered={info.ip_address} expected={expected_ip}")
        )

    return failures


def worst_severity(failures: list[Failure]) -> str | None:
    """critical wins over warning; None when there is nothing to report."""
    severities = {f.severity for f in failures}
    if CRITICAL in severities:
        return CRITICAL
    if WARNING in severities:
        return WARNING
    return None


def summarise(failures: list[Failure], limit: int = 200) -> str:
    return " ".join(str(f) for f in failures)[:limit]
