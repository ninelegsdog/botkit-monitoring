from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import requests

from e2e.client import TelegramTester
from e2e.config import load_scenarios, load_settings

STATUS_DIR = pathlib.Path("/var/backups/botkit/e2e")
AM_URL = "http://localhost:9093/api/v2/alerts"
THROTTLE = 21600


def token_for(bot: str) -> str:
    p = pathlib.Path(f"/home/deploy/{bot}/.env")
    for line in p.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"no token in {bot}")


def send_alert(bot, reason):
    last = STATUS_DIR / f".alerted.{bot}"
    if last.exists() and (pathlib.Path().cwd() is not None):
        pass
    payload = [{"labels": {"alertname": "E2ETestFailed", "severity": "critical", "bot": bot, "service": "botkit-e2e"},
                "annotations": {"summary": f"E2E fail {bot}", "description": reason}}]
    try:
        requests.post(AM_URL, json=payload, timeout=5)
    except Exception:  # noqa: BLE001, S110
        pass


async def run_all(scenarios, settings, status_dir):
    status_dir.mkdir(parents=True, exist_ok=True)
    async with TelegramTester(settings) as t:
        problems = 0
        for bot, sc in scenarios.items():
            err = ""
            try:
                username = t.get_bot_username(token_for(bot))
                replies = await t.run_scenario(username, sc.steps, settings.timeout)
                ok = all(exp in rep for exp, rep in zip([s.expect for s in sc.steps], replies))
            except Exception as e:  # noqa: BLE001
                ok = False
                err = str(e)
            if ok:
                (status_dir / f"{bot}.ok").write_text("ok")
                (status_dir / f"{bot}.fail").unlink(missing_ok=True)
                print(f"OK {bot}")
            else:
                (status_dir / f"{bot}.fail").write_text("fail")
                (status_dir / f"{bot}.ok").unlink(missing_ok=True)
                send_alert(bot, err if not ok else "unexpected reply")
                print(f"FAIL {bot}")
                problems += 1
        return problems


def main():
    settings = load_settings()
    scenarios = load_scenarios(os.environ.get("E2E_SCENARIOS", "scenarios.yml"))
    problems = asyncio.run(run_all(scenarios, settings, STATUS_DIR))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
