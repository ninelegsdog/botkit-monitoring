from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

if TYPE_CHECKING:
    from e2e.config import Settings

try:  # Telethon needs PySocks for a SOCKS proxy; hosts without one skip it.
    import socks  # noqa: F401

    _SOCKS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - depends on the venv
    _SOCKS_AVAILABLE = False


def _proxy_tuple(spec: str) -> tuple[str, str, int] | None:
    """Translate "socks5h:127.0.0.1:11080" into telethon's (type, host, port).

    None for an empty spec, so connecting directly stays the default. A malformed
    spec raises here rather than falling back to a direct connection: a typo in the
    proxy setting must not silently produce an attempt that cannot work.
    """
    if not spec:
        return None
    kind, _, rest = spec.partition(":")
    host, _, port = rest.partition(":")
    if kind not in {"socks5", "socks5h"} or not host or not port.isdigit():
        msg = f"E2E_PROXY must look like socks5h:host:port, got {spec!r}"
        raise ValueError(msg)
    return kind, host, int(port)


class TelegramTester:
    """Drives the fleet as a user account.

    Authentication is explicit and checked: `connect()` alone says nothing about
    whether the session is usable, and an unauthorized session would sit in
    `run_scenario` until the first timeout instead of failing at the reason.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        proxy = _proxy_tuple(settings.proxy)
        if proxy is not None and not _SOCKS_AVAILABLE:
            # Telethon speaks SOCKS only through PySocks. Checked here so the
            # failure names the missing package instead of arriving much later
            # as a connect timeout against the proxy port.
            msg = "E2E_PROXY is set but PySocks is missing from the venv"
            raise RuntimeError(msg)
        self.client = TelegramClient(
            str(settings.session_path),
            settings.api_id,
            settings.api_hash,
            device_model=settings.device_model,
            system_version=settings.system_version,
            app_version=settings.app_version,
            proxy=proxy,
        )
        self._usernames: dict[str, str] = {}

    async def __aenter__(self) -> TelegramTester:
        await self.connect()
        return self

    async def __aexit__(self, *_a: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Connect and refuse to continue on an unusable session.

        A missing phone is only acceptable for an already-authorized session;
        that is what lets the E2E host run without keeping TG_PHONE.
        """
        await self.client.connect()
        if await self.client.is_user_authorized():
            return
        if not self.settings.phone:
            msg = (
                f"session {self.settings.session_path} is not authorized and no phone is "
                "configured; run first_login.py on an admin machine instead of storing TG_PHONE here"
            )
            raise RuntimeError(msg)
        try:
            await self.client.send_code_request(self.settings.phone)
        except SessionPasswordNeededError as exc:  # pragma: no cover - depends on account 2FA
            msg = "account requires 2FA; complete sign-in manually with first_login.py"
            raise RuntimeError(msg) from exc
        await self.client.sign_in(phone=self.settings.phone, code=lambda: input("Telegram code: "))
        if not await self.client.is_user_authorized():
            msg = "sign-in completed without producing an authorized session"
            raise RuntimeError(msg)

    async def disconnect(self) -> None:
        await self.client.disconnect()

    def prime_usernames(self, mapping: dict[str, str]) -> None:
        """Cache bot directory name -> username for the duration of the run."""
        self._usernames = dict(mapping)

    def username_for(self, bot: str) -> str:
        if bot not in self._usernames:
            msg = f"no username known for {bot}; bots.yml must be loaded before the run"
            raise RuntimeError(msg)
        return self._usernames[bot]

    async def run_scenario(self, username: str, steps: list[Any], timeout: int) -> list[str]:
        out: list[str] = []
        for st in steps:
            top = await self.client.get_messages(username, limit=1)
            seen = top[0].id if top else 0
            await self.client.send_message(username, st.send)
            reply, seen = await self._wait_reply(username, timeout, seen)
            out.append(reply)
        return out

    async def _wait_reply(self, username: str, timeout: int, after_id: int) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for m in await self.client.get_messages(username, limit=5):
                if m.id <= after_id:
                    continue
                if not m.out and m.text:
                    return m.text, m.id
            await asyncio.sleep(1.5)
        msg = f"no fresh reply from {username}"
        raise TimeoutError(msg)
