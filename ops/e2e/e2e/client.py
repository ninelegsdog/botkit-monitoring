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
        seen = 0
        for st in steps:
            top = await self.client.get_messages(username, limit=1)
            seen = top[0].id if top else 0
            await self.client.send_message(username, st.send)
            reply, seen = await self._wait_reply(username, timeout, seen)
            out.append(reply)
        return out

    async def _wait_reply(self, username, timeout, after_id):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for m in await self.client.get_messages(username, limit=5):
                if m.id <= after_id:
                    continue
                if m.out is False and m.text:
                    return m.text, m.id
            await asyncio.sleep(1.5)
        raise TimeoutError(f"no reply from {username}")
