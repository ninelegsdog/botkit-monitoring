from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

from e2e.client import TelegramTester
from e2e.config import Settings, load_bots, load_scenarios, load_settings

ALERT_THROTTLE_S = 3600  # не чаще 1 алерта на бота за 1ч
ALERT_DESCRIPTION_LIMIT = 200
HTTP_OK = 200
HTTP_MULTI_STATUS = 300
# Distinct codes on purpose: an operator reading the journal or `systemctl`
# has to be able to tell "the fleet failed" from "the tests never ran".
EXIT_TESTS_FAILED = 1
EXIT_NO_ALERT_TRANSPORT = 2


def post_alert(url: str, payload: list[dict], timeout: int = 5) -> bool:
    """POST to Alertmanager. Returns False instead of raising.

    A failed alert must not mask the test result that triggered it, so the
    reason is reported and the caller keeps going.
    """
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return HTTP_OK <= resp.status < HTTP_MULTI_STATUS


def build_alert(bot: str, reason: str) -> list[dict]:
    return [
        {
            "labels": {
                "alertname": "E2ETestFailed",
                "severity": "warning",  # E2: не critical by default
                "bot": bot,
                "service": "botkit-e2e",
            },
            "annotations": {"summary": f"E2E fail {bot}", "description": reason[:ALERT_DESCRIPTION_LIMIT]},
        }
    ]


def _alert_marker(settings: Settings, bot: str, channel: str):
    return settings.status_dir / f".alerted.{channel}.{bot}"


def _alert_throttled(settings: Settings, bot: str, channel: str) -> bool:
    """True when this channel already alerted about this bot inside the window.

    Each channel keeps its own marker on purpose. A shared marker would let a
    successful Telegram send suppress the Alertmanager post, or the reverse,
    for the rest of the hour.
    """
    last = _alert_marker(settings, bot, channel)
    return last.exists() and time.time() - last.stat().st_mtime < ALERT_THROTTLE_S


def _mark_alerted(settings: Settings, bot: str, channel: str) -> None:
    _alert_marker(settings, bot, channel).write_text(str(time.time()))


def send_alert(settings: Settings, bot: str, reason: str) -> bool:
    """POST to Alertmanager. An unconfigured URL is a loud no-op, not a silent success."""
    if not settings.alert_url:
        print(f"FATAL no E2E_ALERT_URL configured; cannot alert on {bot} failure (fail-loud)")
        return False
    if _alert_throttled(settings, bot, "am"):
        return False
    try:
        ok = post_alert(settings.alert_url, build_alert(bot, reason))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"WARN alert send failed: {exc}")
        return False
    if ok:
        _mark_alerted(settings, bot, "am")
    return ok


async def notify_saved_messages(tester, settings: Settings, bot: str, reason: str) -> bool:
    """Page the owner through the E2E session's own Saved Messages.

    Alertmanager listens only on the prod loopback and prod forbids TCP
    forwarding (AllowTcpForwarding no), so this host has no route to it. The
    user session is already authorized and is the only channel that needs no
    new credential, so the alert is written to "me": no bot token, no new
    inbound port, nothing to store.
    """
    if _alert_throttled(settings, bot, "tg"):
        return False
    text = (
        f"E2E FAIL {bot}\n"
        f"{reason[:ALERT_DESCRIPTION_LIMIT]}\n"
        "runner: botkit-e2e (channel: Saved Messages)"
    )
    try:
        await tester.client.send_message("me", text)
    except Exception as exc:  # a failed page must not mask the test result
        print(f"WARN Saved Messages notify failed for {bot}: {exc}")
        return False
    _mark_alerted(settings, bot, "tg")
    return True


async def alert_on_failure(tester, settings: Settings, bot: str, reason: str) -> str:
    """Try every enabled channel; return which ones actually delivered."""
    used = []
    if settings.alert_url and send_alert(settings, bot, reason):
        used.append("alertmanager")
    if settings.alert_telegram and await notify_saved_messages(tester, settings, bot, reason):
        used.append("telegram")
    return "+".join(used) if used else "NONE"


async def notify_run_summary(tester, settings: Settings, problems: int, total: int) -> bool:
    """Report the run's own outcome. This is the watchdog half of alerting.

    A per-bot failure already pages by itself, so silence used to be ambiguous:
    a runner that died halfway and a fleet that is perfectly healthy both said
    nothing. One message per run makes the absence of a message meaningful.
    """
    verdict = "all passed" if problems == 0 else f"{problems} of {total} FAILED"
    text = (
        f"E2E run finished: {total - problems}/{total} {verdict}\n"
        "runner: botkit-e2e (channel: Saved Messages)"
    )
    try:
        await tester.client.send_message("me", text)
    except Exception as exc:  # a missed heartbeat must not rewrite the verdict
        print(f"WARN run summary not delivered: {exc}")
        return False
    print(f"watchdog: summary delivered ({verdict})")
    return True


async def run_all(scenarios, settings: Settings) -> int:
    settings.status_dir.mkdir(parents=True, exist_ok=True)
    bots = load_bots(settings.bots_file)
    problems = 0
    async with TelegramTester(settings) as t:
        t.prime_usernames(bots)
        for bot, sc in scenarios.items():
            err = ""
            try:
                username = t.username_for(bot)
                replies = await t.run_scenario(username, sc.steps, settings.timeout)
                expected = [s.expect for s in sc.steps]
                matched = all(exp in rep for exp, rep in zip(expected, replies, strict=False))
                ok = len(replies) == len(expected) and matched
            except Exception as e:  # a per-bot failure must not abort the fleet
                ok = False
                err = str(e)
            if ok:
                (settings.status_dir / f"{bot}.ok").write_text("ok")
                (settings.status_dir / f"{bot}.fail").unlink(missing_ok=True)
                (settings.status_dir / f".alerted.{bot}").unlink(missing_ok=True)
                print(f"OK {bot}")
            else:
                (settings.status_dir / f"{bot}.fail").write_text("fail")
                (settings.status_dir / f"{bot}.ok").unlink(missing_ok=True)
                sent = await alert_on_failure(t, settings, bot, err or "unexpected reply")
                print(f"FAIL {bot} ({err or 'unexpected reply'}) alert={sent}")
                problems += 1
        if settings.watchdog:
            await notify_run_summary(t, settings, problems, len(scenarios))
    return problems


def main() -> None:
    settings = load_settings()
    if not (settings.alert_url or settings.alert_telegram):
        if not settings.allow_no_alert:
            print(
                "FATAL no alert channel: E2E_ALERT_URL is empty and "
                "E2E_ALERT_TELEGRAM is off, so a failure would page nobody and\n"
                "a pass would prove nothing. Refusing to run. Enable one, or\n"
                "set E2E_ALLOW_NO_ALERT=1 to run deliberately unactioned.",
                file=sys.stderr,
            )
            sys.exit(EXIT_NO_ALERT_TRANSPORT)
        print(
            "WARNING running with every alert channel disabled because "
            "E2E_ALLOW_NO_ALERT=1: failures will not page anyone.",
            file=sys.stderr,
        )
    scenarios = load_scenarios(settings.scenarios_file)
    problems = asyncio.run(run_all(scenarios, settings))
    sys.exit(EXIT_TESTS_FAILED if problems else 0)


if __name__ == "__main__":
    main()
