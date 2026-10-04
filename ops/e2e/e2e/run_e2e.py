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


def send_alert(settings: Settings, bot: str, reason: str) -> bool:
    """Send a throttled failure alert. An unconfigured URL is a loud no-op, not a silent success."""
    if not settings.alert_url:
        print(f"FATAL no E2E_ALERT_URL configured; cannot alert on {bot} failure (fail-loud)")
        return False
    last = settings.status_dir / f".alerted.{bot}"
    now = time.time()
    if last.exists() and now - last.stat().st_mtime < ALERT_THROTTLE_S:
        return False
    try:
        ok = post_alert(settings.alert_url, build_alert(bot, reason))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"WARN alert send failed: {exc}")
        return False
    if ok:
        last.write_text(str(now))
    return ok


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
                sent = send_alert(settings, bot, err or "unexpected reply")
                print(f"FAIL {bot} ({err or 'unexpected reply'}) alert={sent}")
                problems += 1
    return problems


def main() -> None:
    settings = load_settings()
    if not settings.alert_url:
        if not settings.allow_no_alert:
            print(
                "FATAL no alert transport: E2E_ALERT_URL is empty, so a failure\n"
                "would page nobody and a pass would prove nothing. Refusing to\n"
                "run. Set E2E_ALERT_URL, or set E2E_ALLOW_NO_ALERT=1 to run\n"
                "deliberately unactioned.",
                file=sys.stderr,
            )
            sys.exit(EXIT_NO_ALERT_TRANSPORT)
        print(
            "WARNING E2E_ALLOW_NO_ALERT=1 with E2E_ALERT_URL empty: "
            "failures will not page anyone.",
            file=sys.stderr,
        )
    scenarios = load_scenarios(settings.scenarios_file)
    problems = asyncio.run(run_all(scenarios, settings))
    sys.exit(EXIT_TESTS_FAILED if problems else 0)


if __name__ == "__main__":
    main()
