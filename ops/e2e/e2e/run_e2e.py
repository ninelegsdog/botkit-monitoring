from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import requests

from e2e.client import TelegramTester
from e2e.config import Settings, load_scenarios, load_settings

AM_URL = "http://localhost:9093/api/v2/alerts"
THROTTLE_S = 3600  # не чаще 1 алерта на бота за 1ч (E2)


def token_for(bot: str, bots_dir: pathlib.Path) -> str:
    p = bots_dir / f"{bot}/.env"
    for line in p.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"no TELEGRAM_BOT_TOKEN in {p}")


def send_alert(bot: str, reason: str, status_dir: pathlib.Path) -> bool:
    last = status_dir / f".alerted.{bot}"
    now = time.time()
    if last.exists() and now - last.stat().st_mtime < THROTTLE_S:
        return False
    payload = [
        {
            "labels": {
                "alertname": "E2ETestFailed",
                "severity": "warning",  # E2: не critical by default
                "bot": bot,
                "service": "botkit-e2e",
            },
            "annotations": {"summary": f"E2E fail {bot}", "description": reason[:200]},
        }
    ]
    try:
        requests.post(AM_URL, json=payload, timeout=5)
        last.write_text(str(now))
        return True
    except Exception as exc:
        print(f"WARN alert send failed: {exc}")
        return False


async def run_all(scenarios, settings: Settings, status_dir: pathlib.Path) -> int:
    status_dir.mkdir(parents=True, exist_ok=True)
    problems = 0
    async with TelegramTester(settings) as t:
        for bot, sc in scenarios.items():
            err = ""
            try:
                username = t.get_bot_username(token_for(bot, settings.bots_dir))
                replies = await t.run_scenario(username, sc.steps, settings.timeout)
                expected = [s.expect for s in sc.steps]
                matched = all(exp in rep for exp, rep in zip(expected, replies, strict=False))
                ok = len(replies) == len(expected) and matched
            except Exception as e:
                ok = False
                err = str(e)
            if ok:
                (status_dir / f"{bot}.ok").write_text("ok")
                (status_dir / f"{bot}.fail").unlink(missing_ok=True)
                (status_dir / f".alerted.{bot}").unlink(missing_ok=True)
                print(f"OK {bot}")
            else:
                (status_dir / f"{bot}.fail").write_text("fail")
                (status_dir / f"{bot}.ok").unlink(missing_ok=True)
                sent = send_alert(bot, err or "unexpected reply", status_dir)
                print(f"FAIL {bot} ({err or 'unexpected reply'}) alert={sent}")
                problems += 1
    return problems


def main() -> None:
    settings = load_settings()
    scenarios = load_scenarios(settings.scenarios_file)
    problems = asyncio.run(run_all(scenarios, settings, settings.status_dir))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
