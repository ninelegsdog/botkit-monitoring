# Руководство: получение данных для Task 2, 5, 7

Все три задачи завязаны на **один и тот же набор кредов** — userbot-аккаунт Telegram.
Остальное берётся из инфраструктуры прод-сервера.

## 1. Выделенный Telegram-аккаунт
Нужен отдельный «тестовый» аккаунт (не ваш основной), потому что:
- userbot сидит в личке у ботов и шлёт им сообщения;
- сессия привязывается к номеру телефона и `api_id/api_hash`.

Создайте аккаунт (номер SIM/виртуальный номер, который примет SMS от Telegram).
Запомните **номер телефона** — пойдёт в `TG_PHONE`.

## 2. `api_id` и `api_hash` (my.telegram.org)
1. Откройте https://my.telegram.org с этого аккаунта (войдите по тому же номеру).
2. **API development tools** → заполните форму (App title, Short name — любые,
   напр. `botkit-e2e`, платформа `Desktop`, описание любое).
3. После создания увидите **`api_id`** (число) и **`api_hash`** (строка hex).
   Скопируйте оба.

> Это «пароль уровня приложения» — держите в secret, не коммитьте.

## 3. Файл `.env.e2e`
На проде (или где будет крутиться userbot) создайте:

```
TG_API_ID=1234567
TG_API_HASH=abcdef0123456789abcdef0123456789
TG_PHONE=+79991234567
E2E_TIMEOUT=20
```

Шаблон уже есть в плане — `ops/e2e/.env.e2e.example`. Скопируйте в `.env.e2e` и
заполните. Файл в `.gitignore` (по Global Constraints), **не коммитить**.

## 4. Первый логин userbot (создание сессии)
Перед Task 5/7 нужно один раз залогинить аккаунт — telethon сохранит
`botkit-e2e.session`:

```bash
cd ops/e2e
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env.e2e; set +a
python -c "import asyncio, os; from e2e.client import TelegramTester; from e2e.config import load_settings; \
async def m(): \
  t=TelegramTester(load_settings()); await t.client.start(phone=os.environ['TG_PHONE']); print('OK') \
asyncio.run(m())"
```

- При первом запуске telethon спросит **код из SMS** и, возможно, **2FA-пароль**.
- После успеха появится `botkit-e2e.session` (в `.gitignore`, не коммитить).

## 5. Доступ к токенам ботов (для Task 5/7)
Скрипт `run_e2e.py` читает `TELEGRAM_BOT_TOKEN` из `/home/deploy/<bot>/.env`.
Нужен доступ к серверу прод под `root`:

```bash
ssh root@2.27.204.95
ls /home/deploy/                      # должны быть 9 каталогов botkit-*
grep -h TELEGRAM_BOT_TOKEN /home/deploy/botkit-*/.env   # проверка наличия токенов
```

Юзернеймы ботов достаются автоматически через Bot API `getMe` — вручную ничего
вводить не надо. Для отладки можно проверить:

```bash
for f in /home/deploy/botkit-*/.env; do
  tok=$(grep TELEGRAM_BOT_TOKEN "$f" | cut -d= -f2)
  curl -s "https://api.telegram.org/bot$tok/getMe" | grep -o '"username":"[^"]*"'
done
```

## 6. Alertmanager (для Task 7, валидация алерта)
На проде должен быть доступен `localhost:9093`:

```bash
curl -s localhost:9093/api/v2/alerts | head
```

Если нет — алерт не уйдёт (скрипт глушит ошибку post, см. `send_alert`),
но `.fail`-файл всё равно создастся.

## Чек-лист готовности (всё для 2/5/7)
- [ ] Выделенный TG-аккаунт + номер (`TG_PHONE`)
- [ ] `api_id` + `api_hash` из my.telegram.org
- [ ] `.env.e2e` заполнен
- [ ] `botkit-e2e.session` создан (первый логин прошёл)
- [ ] root-доступ к `/home/deploy/*/.env` на проде
- [ ] Alertmanager отвечает на `localhost:9093`

**Без пп. 1–4 юнит-тесты Task 1/3/4 пройдут, но Task 2 (реальный логин),
Task 5 (live pytest) и Task 7 (прогон) — нет.**
