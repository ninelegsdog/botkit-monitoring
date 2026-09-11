from __future__ import annotations

import asyncio
import sys

from e2e.client import TelegramTester
from e2e.config import load_settings


async def main() -> int:
    settings = load_settings()
    async with TelegramTester(settings) as t:
        if await t.client.is_user_authorized():
            print(f"AUTHORIZED session={settings.session_file}")
            return 0
        print(f"Need one-time login as {settings.phone} ...")
        await t.client.start(phone=settings.phone)
        if await t.client.is_user_authorized():
            print(f"AUTHORIZED session={settings.session_file}")
            return 0
        print("NOT AUTHORIZED")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
