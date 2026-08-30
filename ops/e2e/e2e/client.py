from __future__ import annotations

import asyncio

import requests
from telethon import TelegramClient

API = "https://api.telegram.org/bot{token}/getMe"

def bot_getme(token: str) -> dict:
    r = requests.get(API.format(token=token), timeout=10)
    r.raise_for_status()
    return r.json()["result"]

class TelegramTester:
    def __init__(self, settings):
        self.settings = settings
        self.client = TelegramClient("botkit-e2e", settings.api_id, settings.api_hash)

    async def __aenter__(self):
        await self.client.start(phone=self.settings.phone)
        return self

    async def __aexit__(self, *a):
        await self.client.disconnect()

    @staticmethod
    def get_bot_username(token: str) -> str:
        return bot_getme(token)["username"]

    async def run_scenario(self, username, steps, timeout):
        out = []
        for st in steps:
            await self.client.send_message(username, st.send)
            out.append(await self._wait_reply(username, timeout))
        return out

    async def _wait_reply(self, username, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for m in await self.client.get_messages(username, limit=3):
                if m.out is False and m.text:
                    return m.text
            await asyncio.sleep(1.5)
        raise TimeoutError(f"no reply from {username}")
