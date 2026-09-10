# ops/e2e — сквозное тестирование живой системы ботов

Два слоя проверок. Всё запускается на проде; результат — в этом репо (git `main`,
пуш в GH через bundle-флоу).

## Layer A: инфра-смоуки (bash, без внешних аккаунтов)

| скрипт | что проверяет |
|--------|---------------|
| `smoke_all.sh` | полный путь деплоя по каждому боту: webhook с секретом → 200; webhook без секрета → 401 (negative-auth); `/health` → 200; запись об update в логе aiogram; контейнер up |
| `e2e-alerting.sh` | round trip алерт→Telegram→resolve через правило `E2E_ProbeGone` (`absent()` синтетической метрики) + SIGHUP prometheus; контролирует `alertmanager_notifications_total`; self-cleaning через trap |
| `backup-restore-e2e.sh` | свежесть статус-файлов `<bot>.ok` (<8ч) + `/root/restore_test.sh` для всех 9 ботов (sqlite integrity + redis RDB во временном контейнере) |

Запуск:
```bash
cd /home/deploy/botkit-monitoring && ./ops/e2e/smoke_all.sh && ./ops/e2e/backup-restore-e2e.sh
./ops/e2e/e2e-alerting.sh   # ~5 мин, сам чистится
```
Exit 0 = PASS. `curl -k`: сертификат бота самоподписанный (так задумано).

## Layer B: конверсия с ботами через Telegram (telethon userbot)

Спецификация — `PLAN.md` (единый источник). Пакет `e2e/` + `scenarios.yml` +
systemd-таймер. Реально шлёт `/start`/`/help` в каждого бота с выделенного
Telegram-аккаунта и сверяет ответы с ожиданиями. Ронляции → статус-файлы
`/var/backups/botkit/e2e/*.{ok,fail}` + алерт в Alertmanager
(`E2ETestFailed`, сервис `botkit-e2e`, троттлинг 6ч).

Статус: код + тесты готовы (`pytest` 5 passed, live-тесты skip без кредов).
Нужно для запуска: `.env.e2e` (`TG_API_ID`, `TG_API_HASH`, `TG_PHONE` — см.
`SETUP_GUIDE.md`), `python3-venv` на проде, systemd-юнит.

```bash
cd /home/deploy/botkit-monitoring/ops/e2e
./venv/bin/pytest -q                      # unit-тесты (без кредов)
./venv/bin/python -m e2e.run_e2e          # живой прогон (нужны креды)
```

## CI-запуск Layer A
`.github/workflows/e2e-smoke.yml` — `workflow_dispatch`, секреты репо:
`SSH_KEY` (приватный deploy-ключ из prod authorized_keys), `PROD_HOST`,
`PROD_USER`.
## Прогоны (2026-09-10, live-прод)
- smoke_all.sh: PASS 9/9 (auth 200 / noauth 401 / health 200 / loghit / up).
- e2e-alerting.sh: PASS (round trip Prometheus->AM->Telegram firing; resolve->Telegram;
  подтверждено счётчиками AM + отсутствием active-алерта). Оператор получил 2 сообщения.
  Уникальный alertname на прогон — иначе repeat_interval=4h давит повторное уведомление.
- backup-restore-e2e.sh: PASS — выявил и помог починить провал sqlite-экспортов
  (docker-cp из контейнера -> неверный путь). RPO восстановлен до окна 6ч.
