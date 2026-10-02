"""One-time interactive login for the E2E user account.

Run this on the admin machine, not on the E2E host: it needs the phone number
and the 2FA prompt, and under strategy C neither is kept on the host.

The resulting .session file is the account credential. Transfer it to the
E2E host, verify the checksum, then delete the local copy.
"""

from __future__ import annotations

import asyncio
import sys

from e2e.client import TelegramTester
from e2e.config import load_settings


async def main() -> int:
    settings = load_settings()
    if not settings.phone:
        print("TG_PHONE is required for the first login. It is not stored on the E2E host.")
        return 2

    tester = TelegramTester(settings)
    # connect() refuses an unauthorized session when no phone is present and
    # signs in when one is - which is the whole point of this script, and why
    # it must not go through the host-side runner path.
    await tester.connect()
    try:
        if not await tester.client.is_user_authorized():
            print(f"NOT AUTHORIZED session={settings.session_path}")
            return 1
        print(f"AUTHORIZED session={settings.session_path}")
        return 0
    finally:
        await tester.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
