#!/usr/bin/env python3
"""Verify that Telegram can actually deliver updates to every bot.

A healthy container is not a delivered update. On 26.09.2026 all nine bots served
/health 200 with green CI for fifteen days while Telegram refused every delivery,
because the webhooks still carried a pinned self-signed certificate. This check
asks the only authority that matters — the Bot API — whether the delivery contract
holds, and refuses to rely on a locally-issued health probe for that verdict.

Checks C1/C2/C3/C4/C5/C6/C8 live in webhook_check_lib (unit-tested); C7 (the
public certificate must verify for a third party, without insecure_skip_verify)
is performed here against the public endpoint.

Run on the production host as root from a systemd timer. Exit 0 when every bot
passes, 1 otherwise. Logs to /var/log/botkit-webhook-check.log and raises
BotkitWebhookDeliveryFailed in Alertmanager (throttled per bot).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webhook_check_lib import (
    CRITICAL,
    Failure,
    WebhookInfo,
    WebhookInfoError,
    apply_fault,
    evaluate,
    parse_webhook_info,
    summarise,
    worst_severity,
)

ENV_ROOT = Path("/usr/local/etc/botkit")
LOG_PATH = Path("/var/log/botkit-webhook-check.log")
STATE_ROOT = Path("/var/lib/botkit-webhook-check")
ALERT_ROOT = Path("/var/backups/botkit-webhook-check/alerted")
AM_URL = "http://127.0.0.1:9093/api/v2/alerts"
ALERT_THROTTLE_S = 3600
ALERT_HOLD_H = 6
HTTP_OK = 200
API_TIMEOUT_S = 15

FALLBACK_FLEET = (
    "bookingbot:8081 leadgen:8082 store:8083 support:8084 membership:8085 "
    "pricesentry:8086 docuflow:8087 delivery:8088 reminder:8089"
)


@dataclass(frozen=True)
class Bot:
    name: str
    port: int


@dataclass(frozen=True)
class Contract:
    fleet: list[Bot]
    domain: str
    ip: str
    source: str


def _readable_file(path: Path) -> bool:
    """is_file() that treats an unreadable parent as absent instead of raising."""
    try:
        return path.is_file()
    except OSError:
        return False


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_contract(explicit: str | None = None) -> Contract:
    """Resolve fleet, domain and ip from fleet.env, falling back to built-ins.

    Production installs fleet.env next to the script; the fallback keeps the check
    running if that copy is ever missing.
    """
    here = Path(__file__).resolve().parent
    candidates = ([Path(explicit)] if explicit else []) + [
        here.parent / "lib" / "fleet.env",
        Path("/root/botkit-webhook-check/fleet.env"),
    ]

    fleet_raw: str | None = None
    domain = "ninelegsbots.duckdns.org"
    ip = "2.27.204.95"
    source = "builtin-fallback"
    for path in candidates:
        if not _readable_file(path):
            continue
        values = _parse_env_file(path)
        if not values:
            continue
        fleet_raw = values.get("FLEET", fleet_raw)
        domain = values.get("WEBHOOK_DOMAIN", domain)
        ip = values.get("WEBHOOK_IP", ip)
        source = str(path)
        break

    bots = [Bot(name=n, port=int(p)) for n, _, p in (e.partition(":") for e in (fleet_raw or FALLBACK_FLEET).split())]
    return Contract(fleet=bots, domain=domain, ip=ip, source=source)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Log:
    def __init__(self, path: Path, echo: bool = True) -> None:
        self.path = path
        self.echo = echo

    def __call__(self, message: str) -> None:
        line = f"{now_iso()} {message}"
        if self.echo:
            print(line)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            print(f"{now_iso()} LOG WRITE FAILED: {exc}", file=sys.stderr)


def read_token(bot: str) -> str:
    env_file = ENV_ROOT / f"{bot}.env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise FileNotFoundError(f"TELEGRAM_BOT_TOKEN not found in {env_file}")


def read_secret(bot: str) -> str:
    env_file = ENV_ROOT / f"{bot}.env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("TELEGRAM_WEBHOOK_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def fetch_webhook_info(token: str, timeout: int = API_TIMEOUT_S) -> str:
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    context = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
        return response.read().decode("utf-8")


def check_public_tls(domain: str, timeout: int = 10) -> str | None:
    """Return None when a third party can verify our certificate, else the reason.

    Deliberately no insecure_skip_verify: an untrusted certificate is exactly the
    failure this whole check exists to catch.
    """
    try:
        context = ssl.create_default_context()
        with (
            socket.create_connection((domain, 443), timeout=timeout) as plain,
            context.wrap_socket(plain, server_hostname=domain) as wrapped,
        ):
            wrapped.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        return f"certificate verify failed: {exc.verify_message or exc}"
    except ssl.SSLError as exc:
        return f"tls error: {exc}"
    except OSError as exc:
        return f"connect error: {exc}"
    return None


def read_pending(bot: str) -> int | None:
    path = STATE_ROOT / "pending" / bot
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def write_pending(bot: str, value: int) -> None:
    path = STATE_ROOT / "pending" / bot
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value))


def _post_alerts(payload: list[dict]) -> str | None:
    """Post alerts to Alertmanager. Returns an error string, or None on success."""
    try:
        request = urllib.request.Request(
            AM_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != HTTP_OK:
                return f"HTTP {response.status}"
    except (urllib.error.URLError, OSError) as exc:
        return str(exc)
    return None


def alert_labels(bot: str) -> dict[str, str]:
    return {
        "alertname": "BotkitWebhookDeliveryFailed",
        "severity": CRITICAL,
        "bot": bot,
        "service": "botkit-webhook-check",
    }


def send_alert(bot: str, severity: str, reason: str, log: Log) -> None:
    """Raise BotkitWebhookDeliveryFailed and keep it firing until the bot recovers.

    endsAt is pushed into the future on purpose: a bare POST lets Alertmanager
    expire the alert after five minutes, which turns a persistent fifteen-day
    outage into a notification every hour that never reads as "still broken".
    The throttle limits how often receivers are notified, not how long the alert
    stays open; resolve_alert() closes it.
    """
    if severity != CRITICAL:
        log(f"WARN {bot}: {reason} (no alert, severity={severity})")
        return

    marker = ALERT_ROOT / bot
    throttled = False
    with contextlib.suppress(OSError):
        throttled = marker.is_file() and time.time() - marker.stat().st_mtime < ALERT_THROTTLE_S

    now = datetime.now(timezone.utc)
    payload = [
        {
            "labels": alert_labels(bot),
            "annotations": {
                "summary": f"telegram webhook delivery broken: {bot}",
                "description": reason.replace('"', "'")[:200],
            },
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(hours=ALERT_HOLD_H)).isoformat(),
        }
    ]
    error = _post_alerts(payload)
    if error:
        log(f"ALERT send FAILED ({bot}): {error}")
        return
    if throttled:
        log(f"ALERT still firing, notification throttled ({bot}): {reason}")
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(now_iso())
    log(f"ALERT sent ({bot}): {reason}")


def resolve_alert(bot: str, log: Log) -> None:
    """Close a previously raised alert as soon as the bot passes again."""
    marker = ALERT_ROOT / bot
    if not _readable_file(marker):
        return
    now = datetime.now(timezone.utc)
    error = _post_alerts(
        [
            {
                "labels": alert_labels(bot),
                "annotations": {"summary": f"telegram webhook delivery restored: {bot}"},
                "startsAt": now.isoformat(),
                "endsAt": now.isoformat(),
            }
        ]
    )
    if error:
        log(f"ALERT resolve FAILED ({bot}): {error} (Alertmanager will expire it)")
        return
    marker.unlink(missing_ok=True)
    log(f"ALERT resolved ({bot})")


def check_bot(bot: Bot, args: argparse.Namespace, domain: str, expected_ip: str, log: Log) -> int:
    """Run every applicable check for one bot. Returns 1 when it is unhealthy."""
    try:
        token = read_token(bot.name)
    except FileNotFoundError as exc:
        reason = str(exc)
        log(f"{bot.name:12s} FAIL C1[critical]: {reason}")
        send_alert(bot.name, CRITICAL, reason, log)
        return 1

    failures: list[Failure] = []
    info: WebhookInfo | None = None
    try:
        info = parse_webhook_info(fetch_webhook_info(token))
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        failures.append(Failure("C1", CRITICAL, f"getWebhookInfo unreachable: {exc}"))
    except WebhookInfoError as exc:
        failures.append(Failure("C1", CRITICAL, f"getWebhookInfo malformed: {exc}"))

    if info is not None:
        if args.fault != "none":
            info = apply_fault(info, args.fault)
        failures += evaluate(
            info,
            expected_url=f"https://{domain}/webhook/{bot.name}",
            expected_ip=expected_ip or None,
            secret_configured=bool(read_secret(bot.name)),
            prev_pending=None if args.quick else read_pending(bot.name),
            quick=args.quick,
        )
        if not args.quick and args.fault == "none":
            write_pending(bot.name, info.pending_update_count)

    severity = worst_severity(failures)
    if severity is None:
        pending = info.pending_update_count if info else "?"
        pinned = info.has_custom_certificate if info else "?"
        log(f"{bot.name:12s} OK (pending={pending} custom_cert={pinned})")
        resolve_alert(bot.name, log)
        return 0

    reason = summarise(failures)
    log(f"{bot.name:12s} FAIL {reason}")
    send_alert(bot.name, str(severity), reason, log)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check that Telegram can deliver updates to every bot")
    parser.add_argument("--quick", action="store_true", help="contract checks only (C1/C2/C5), no TLS, no state")
    parser.add_argument("--fault", default="none", help="inject a failure to exercise the alerting path")
    parser.add_argument("--bot", help="check a single bot")
    parser.add_argument("--fleet-file", help="path to fleet.env")
    parser.add_argument("--domain", help="override WEBHOOK_DOMAIN")
    args = parser.parse_args(argv)

    log = Log(LOG_PATH)
    contract = load_contract(args.fleet_file)
    domain = args.domain or contract.domain
    expected_ip = "" if args.domain else contract.ip
    bots = [b for b in contract.fleet if not args.bot or b.name == args.bot] or [Bot(str(args.bot), 0)]
    if not args.quick:
        STATE_ROOT.joinpath("pending").mkdir(parents=True, exist_ok=True)
        ALERT_ROOT.mkdir(parents=True, exist_ok=True)

    log(f"CHECK start quick={args.quick} fault={args.fault} domain={domain} fleet={contract.source} bots={len(bots)}")

    if not args.quick and args.fault == "none":
        tls_problem = check_public_tls(domain)
        if tls_problem:
            log(f"C7 CRITICAL: {domain}: {tls_problem}")
            for bot in bots:
                send_alert(bot.name, CRITICAL, f"C7 public TLS unusable: {tls_problem}", log)
        else:
            log("C7 OK public TLS verifies for a third party")

    rc = 0
    for bot in bots:
        rc |= check_bot(bot, args, domain, expected_ip, log)

    log(f"CHECK end rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
