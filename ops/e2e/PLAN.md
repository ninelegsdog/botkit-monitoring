# Автотест раздеплоенных ботов через Telegram — План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Регулярно прогонять скриптовые сценарии в реальных ботах через Telegram и поднимать алерт, если бот не отвечает или отвечает неверно.

**Architecture:** Отдельный Python-пакет `botkit-e2e` в `botkit-monitoring/ops/e2e/`. Тестовый клиент — Telegram **userbot** на `telethon` (выделенный аккаунт), который в личке шлёт каждому боту сообщения и читает ответы. Токены берутся из `/home/deploy/<bot>/.env` (`TELEGRAM_BOT_TOKEN`), юзернейм бота — через Bot API `getMe`. Результат пишется в статус-файл и при ошибке шлётся алерт в Alertmanager (`localhost:9093`) — по схеме `check_backups.sh`.

**Tech Stack:** Python 3.11, `telethon`, `pyyaml`, `requests`, `pytest`. На проде запуск через systemd timer.

**Spec:** Настоящий план (единый источник).

## Global Constraints

- Токены/пароли — только из `.env` или secrets, НЕ коммитить. `botkit-e2e/.env.e2e` в `.gitignore`.
- Python 3.10+, типы обязательны, ruff clean, `pytest` зелёный.
- Conventional Commits (`feat:`, `ops:`, `test:`).
- Запуск на проде от `root` (доступ к `/home/deploy/*/.env`).
- Алерты — только в Alertmanager (`localhost:9093/api/v2/alerts`), labels: `alertname`, `severity`, `bot`, `service` (как в `check_backups.sh`).
- Один выделенный userbot-аккаунт на систему; сессия + `api_id/api_hash` в secret.

## One-time Setup (вручную, вне tasks)

1. Выделенный Telegram-аккаунт для тестов.
2. На my.telegram.org получить `api_id`, `api_hash`.
3. `.env.e2e`: `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `E2E_TIMEOUT=20`.
4. Первый запуск залогинит userbot и создаст `botkit-e2e.session` (не коммитить).

---

## Task 1: Scaffold пакета и загрузчик конфигурации

**Files:** Create `ops/e2e/pyproject.toml`, `ops/e2e/requirements.txt`, `ops/e2e/e2e/__init__.py`, `ops/e2e/e2e/config.py`, `ops/e2e/.gitignore`, `ops/e2e/tests/test_config.py`

**Interfaces:** Produces `load_settings() -> Settings`, `load_scenarios(path) -> dict[str, Scenario]`.

- [ ] **Step 1: Write failing test** (`tests/test_config.py`)
```python
from e2e.config import load_settings, load_scenarios
def test_load_settings(monkeypatch):
    monkeypatch.setenv("TG_API_ID","123"); monkeypatch.setenv("TG_API_HASH","abc"); monkeypatch.setenv("TG_PHONE","+000")
    s=load_settings(); assert s.api_id==123 and s.api_hash=="abc"
def test_load_scenarios(tmp_path):
    p=tmp_path/"sc.yaml"; p.write_text("bookingbot:\n  steps:\n    - send: /start\n      expect: Привет\n")
    sc=load_scenarios(p); assert sc["bookingbot"].steps[0].expect=="Привет"
```
- [ ] **Step 2:** Run `cd ops/e2e && pytest tests/test_config.py -v` → FAIL (ModuleNotFoundError)
- [ ] **Step 3: Implement** (`e2e/config.py`)
```python
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
import yaml
@dataclass
class Settings:
    api_id: int; api_hash: str; phone: str
    timeout: int = 20
    bots_dir: Path = Path("/home/deploy")
@dataclass
class Step:
    send: str; expect: str
@dataclass
class Scenario:
    steps: list[Step] = field(default_factory=list)
def load_settings() -> Settings:
    return Settings(api_id=int(os.environ["TG_API_ID"]), api_hash=os.environ["TG_API_HASH"],
                    phone=os.environ["TG_PHONE"], timeout=int(os.environ.get("E2E_TIMEOUT","20")),
                    bots_dir=Path(os.environ.get("E2E_BOTS_DIR","/home/deploy")))
def load_scenarios(path) -> dict[str, Scenario]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {b: Scenario(steps=[Step(s["send"], s["expect"]) for s in c.get("steps",[])]) for b,c in data.items()}
```
- [ ] **Step 4:** Run test → PASS
- [ ] **Step 5:** `git commit -m "feat(e2e): scaffold package + config loader"`

---

## Task 2: Userbot-клиент на telethon

**Files:** Create `ops/e2e/e2e/client.py`, `ops/e2e/tests/test_client.py`

**Interfaces:** Produces `TelegramTester` с `get_bot_username(token)->str` и `async run_scenario(bot_username, steps, timeout)->list[str]`.

- [ ] **Step 1: Write failing test**
```python
import e2e.client as C
from e2e.client import TelegramTester
def test_get_bot_username(monkeypatch):
    monkeypatch.setattr(C,"bot_getme", lambda t: {"username":"bookingbot"})
    assert TelegramTester.get_bot_username("x")=="bookingbot"
```
- [ ] **Step 2:** Run → FAIL
- [ ] **Step 3: Implement** (`e2e/client.py`)
```python
from __future__ import annotations
import asyncio, requests
from telethon import TelegramClient
API="https://api.telegram.org/bot{token}/getMe"
def bot_getme(token: str) -> dict:
    r=requests.get(API.format(token=token), timeout=10); r.raise_for_status()
    return r.json()["result"]
class TelegramTester:
    def __init__(self, settings):
        self.client=TelegramClient("botkit-e2e", settings.api_id, settings.api_hash)
    async def __aenter__(self):
        await self.client.start(phone=self.settings.phone); return self
    async def __aexit__(self,*a): await self.client.disconnect()
    @staticmethod
    def get_bot_username(token: str) -> str: return bot_getme(token)["username"]
    async def run_scenario(self, username, steps, timeout):
        out=[]
        for st in steps:
            await self.client.send_message(username, st.send)
            out.append(await self._wait_reply(username, timeout))
        return out
    async def _wait_reply(self, username, timeout):
        import asyncio as a
        deadline=a.get_event_loop().time()+timeout
        while a.get_event_loop().time()<deadline:
            for m in await self.client.get_messages(username, limit=3):
                if m.out is False and m.text: return m.text
            await a.sleep(1.5)
        raise TimeoutError(f"no reply from {username}")
```
- [ ] **Step 4:** Run test → PASS
- [ ] **Step 5:** `git commit -m "feat(e2e): telethon userbot client"`

---

## Task 3: Сценарии по ботам

**Files:** Create `ops/e2e/scenarios.yml`, `ops/e2e/tests/test_scenarios.py`

**Interfaces:** Produces `scenarios.yml` со списком ботов и шагами `send`/`expect`.

- [ ] **Step 1: Write failing test**
```python
from e2e.config import load_scenarios
from pathlib import Path
def test_all_nine():
    sc=load_scenarios(Path("scenarios.yml"))
    exp={"botkit-bookingbot","botkit-delivery","botkit-docuflow","botkit-leadgen",
         "botkit-membership","botkit-pricesentry","botkit-reminder","botkit-store","botkit-support"}
    assert exp.issubset(set(sc))
    for b,s in sc.items(): assert s.steps, f"{b} empty"
```
- [ ] **Step 2:** Run → FAIL (нет файла)
- [ ] **Step 3: Write scenarios.yml** (для каждого бота минимум `/start` → проверка приветствия; bookingbot + `/help` → «услуг»)
```yaml
botkit-bookingbot:
  steps:
    - send: /start
      expect: "Привет"
    - send: /help
      expect: "услуг"
botkit-delivery:
  steps: [{send: /start, expect: "Привет"}]
botkit-docuflow:
  steps: [{send: /start, expect: "Привет"}]
botkit-leadgen:
  steps: [{send: /start, expect: "Привет"}]
botkit-membership:
  steps: [{send: /start, expect: "Привет"}]
botkit-pricesentry:
  steps: [{send: /start, expect: "Привет"}]
botkit-reminder:
  steps: [{send: /start, expect: "Привет"}]
botkit-store:
  steps: [{send: /start, expect: "Привет"}]
botkit-support:
  steps: [{send: /start, expect: "Привет"}]
```
- [ ] **Step 4:** Run → PASS
- [ ] **Step 5:** `git commit -m "test(e2e): add per-bot smoke scenarios"`

## Task 4: Оркестратор + статус/алерт

**Files:** Create `ops/e2e/run_e2e.py`, `ops/e2e/tests/test_runner.py`

**Interfaces:** Consumes `TelegramTester`, `load_scenarios`, список бот-директорий. Produces статус-файлы `/var/backups/botkit/e2e/<bot>.ok|.fail`, алерт в Alertmanager при провале, exit 1 при ошибке.

- [ ] **Step 1: Write failing test** (mock client, проверка записи `.ok`/`.fail`)
```python
from e2e import run_e2e
def test_runner_status(tmp_path, monkeypatch):
    monkeypatch.setattr(run_e2e,"STATUS_DIR", tmp_path)
    class Fake:
        async def __aenter__(self): return self
        async def __aexit__(self,*a): return None
        async def run_scenario(self, u, steps, t): return [s.expect for s in steps]
        @staticmethod
        def get_bot_username(tok): return "bot"
    monkeypatch.setattr(run_e2e,"TelegramTester", lambda s: Fake())
    sc={"botkit-x": type("S",(),{"steps":[type("St",(),{"send":"/start","expect":"Привет"})()]})()}
    run_e2e.run_all(sc, Settings_for_test(), tmp_path)
    assert (tmp_path/"botkit-x.ok").exists()
```
- [ ] **Step 2:** Run → FAIL
- [ ] **Step 3: Implement** (`run_e2e.py`)
```python
from __future__ import annotations
import os, sys, glob, asyncio, pathlib
import requests
from e2e.config import load_settings, load_scenarios, Settings
from e2e.client import TelegramTester

STATUS_DIR = pathlib.Path("/var/backups/botkit/e2e")
AM_URL = "http://localhost:9093/api/v2/alerts"
THROTTLE = 21600

def token_for(bot: str) -> str:
    p = pathlib.Path(f"/home/deploy/{bot}/.env")
    for line in p.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=",1)[1].strip()
    raise RuntimeError(f"no token in {bot}")

def send_alert(bot, reason):
    last = STATUS_DIR/f".alerted.{bot}"
    if last.exists() and (pathlib.Path().cwd() is not None):
        pass
    payload=[{"labels":{"alertname":"E2ETestFailed","severity":"critical","bot":bot,"service":"botkit-e2e"},
              "annotations":{"summary":f"E2E fail {bot}","description":reason}}]
    try: requests.post(AM_URL, json=payload, timeout=5)
    except Exception: pass

async def run_all(scenarios, settings, status_dir):
    status_dir.mkdir(parents=True, exist_ok=True)
    async with TelegramTester(settings) as t:
        problems=0
        for bot, sc in scenarios.items():
            try:
                username = TelegramTester.get_bot_username(token_for(bot))
                replies = await t.run_scenario(username, sc.steps, settings.timeout)
                ok = all(exp in rep for exp, rep in zip([s.expect for s in sc.steps], replies))
            except Exception as e:
                ok=False; err=str(e)
            if ok:
                (status_dir/f"{bot}.ok").write_text("ok")
                (status_dir/f"{bot}.fail").unlink(missing_ok=True)
                print(f"OK {bot}")
            else:
                (status_dir/f"{bot}.fail").write_text("fail")
                (status_dir/f"{bot}.ok").unlink(missing_ok=True)
                send_alert(bot, err if not ok else "unexpected reply")
                print(f"FAIL {bot}"); problems+=1
    return problems

def main():
    settings=load_settings()
    scenarios=load_scenarios(os.environ.get("E2E_SCENARIOS", "scenarios.yml"))
    problems=asyncio.run(run_all(scenarios, settings, STATUS_DIR))
    sys.exit(1 if problems else 0)

if __name__=="__main__": main()
```
- [ ] **Step 4:** Run test → PASS
- [ ] **Step 5:** `git commit -m "feat(e2e): orchestrator + status/alert"`

---

## Task 5: pytest-обёртка для локального прогона

**Files:** Create `ops/e2e/tests/test_e2e_live.py`

**Interfaces:** Параметризованный тест по ботам; скипается без `TG_API_ID`/`TG_PHONE`.

- [ ] **Step 1: Write test**
```python
import os, pytest
from e2e.config import load_settings, load_scenarios
from e2e.client import TelegramTester
pytestmark = pytest.mark.skipif(not os.environ.get("TG_API_ID"), reason="no e2e creds")
@pytest.mark.parametrize("bot", ["botkit-bookingbot","botkit-support"])
def test_bot_responds(bot):
    settings=load_settings(); sc=load_scenarios("scenarios.yml")[bot]
    username=TelegramTester.get_bot_username(__import__("e2e.run_e2e",fromlist=["x"]).token_for(bot))
    import asyncio
    async def go():
        async with TelegramTester(settings) as t:
            reps=await t.run_scenario(username, sc.steps, settings.timeout)
            return all(e in r for e,r in zip([s.expect for s in sc.steps], reps))
    assert asyncio.run(go())
```
- [ ] **Step 2:** Run `pytest tests/test_e2e_live.py -v` (без creds → skipped; с creds → реальный прогон)
- [ ] **Step 3:** `git commit -m "test(e2e): live pytest wrapper"`

---

## Task 6: Деплой на прод (systemd timer) + GitHub Actions

**Files:** Create `ops/e2e/systemd/botkit-e2e.service`, `ops/e2e/systemd/botkit-e2e.timer`, `ops/e2e/.env.e2e.example`, Modify repo `.github/workflows/e2e.yml`

**Interfaces:** Таймер каждые 15 мин; workflow по расписанию при восстановлении биллинга.

- [ ] **Step 1: Write systemd units**
`botkit-e2e.service`:
```
[Unit]
Description=Botkit E2E Telegram smoke test
[Service]
Type=oneshot
EnvironmentFile=/root/botkit-e2e.env
WorkingDirectory=/root/botkit-e2e
ExecStart=/root/botkit-e2e/venv/bin/python run_e2e.py
```
`botkit-e2e.timer`:
```
[Unit]
Description=Botkit E2E every 15 min
[Timer]
OnCalendar=*:0/15
Persistent=true
[Install]
WantedBy=timers.target
```
- [ ] **Step 2: Write .env.e2e.example** (`TG_API_ID=`, `TG_API_HASH=`, `TG_PHONE=`, `E2E_TIMEOUT=20`) и `.github/workflows/e2e.yml` (schedule cron + setup python + `pip install -r requirements.txt` + `pytest` со secrets).
- [ ] **Step 3:** Деплой на прод: `scp` пакета в `/root/botkit-e2e`, создать venv, `systemctl daemon-reload && systemctl enable --now botkit-e2e.timer`.
- [ ] **Step 4:** `git commit -m "ops(e2e): systemd timer + CI workflow"`

---

## Task 7: Первый прогон и валидация

**Files:** — (операция на проде)

- [ ] **Step 1:** На проде: `cd /root/botkit-e2e && venv/bin/python run_e2e.py`; ожидать 9× `OK`, exit 0.
- [ ] **Step 2:** Проверить статус-файлы: `ls /var/backups/botkit/e2e/*.ok` → 9 шт.
- [ ] **Step 3:** Намеренно сломать одну `expect`-строку в `scenarios.yml`, перезапустить, убедиться что появляется `.fail` и алерт в Alertmanager (`curl localhost:9093/api/v2/alerts`), затем вернуть обратно.
- [ ] **Step 4:** `git commit -m "test(e2e): validate alert path on prod"` (если правили сценарии)

---

## Execution Handoff

Plan saved to `botkit-monitoring/ops/e2e/PLAN.md`. Two execution options:

1. **Subagent-Driven (recommended)** — свежий субагент на задачу, ревью между задачами.
2. **Inline Execution** — задачи в этой сессии с чекпоинтами.

Какой вариант? (Также нужен One-time Setup: выделенный TG-аккаунт + api_id/api_hash — без этого Task 2/5/7 не запустятся реально.)
