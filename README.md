# botkit-monitoring

Мониторинг 8 ботов BotKit: Prometheus + Alertmanager + Grafana (host-network, только 127.0.0.1).

## Стек
- **Prometheus** `127.0.0.1:9090` — скрайпит `/metrics` ботов на портах 8081–8088
- **Alertmanager** `127.0.0.1:9093` — алерты → Telegram (`telegram_configs`)
- **Grafana** `127.0.0.1:3000` — дашборд «BotKit Overview» (переменная instance), доступ через SSH-туннель
- **Loki** `127.0.0.1:3100` — логи Docker-контейнеров (Promtail), хранение → MinIO S3 (Sprint 8)
- **Tempo** `127.0.0.1:3200` — трассы OTLP (otel-collector), хранение → MinIO S3 (Sprint 8)
- **MinIO** `127.0.0.1:9000/9001` — S3-хранилище Loki/Tempo, только loopback

## Хранилище объектов (MinIO, Sprint 8)
- Бакеты: `loki`, `tempo`. Креды `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` — в `.env` (НЕ в git).
- Loki: бэкенд `s3` (бакет `loki`), схема v13 (boltdb-shipper): чанки и индекс — в S3.
- Tempo: бэкенд `s3` (бакет `tempo`).
- Креды в конфиги — через `-config.expand-env=true` (Loki 3.1 / Tempo 2.6):
  compose передаёт `$${MINIO_ROOT_USER}` / `$${MINIO_ROOT_PASSWORD}` из `.env`.
- Hardening MinIO: `--address/--console-address 127.0.0.1`, `cap_drop: ALL`, `read_only: true`,
  `no-new-privileges: true`, tmpfs `/tmp` + `/home/minio`. Наружу MinIO НЕ торчит.
- Миграция 2026-09-05: Loki переведён filesystem → S3 целиком. Старые локальные чанки
  (`loki-data/chunks`, ~12 МБ) остались на диске, но не индексируются — для отката сохраняются.
  Loki 3.1 не поддерживает двухбэковый dual-period (common := один storage); при миграции
  старые чанки в запросах не видны.

## Деплой на VPS
```
cd /opt/monitoring
git pull
cp .env.example .env  # TG_BOT_TOKEN, TG_CHAT_ID, GRAFANA_ADMIN_PASSWORD, MINIO_ROOT_USER, MINIO_ROOT_PASSWORD
docker compose up -d
```

## Алерты
| Alert | Условие | For |
|-------|---------|-----|
| BotDown | `up{job="botkit"} == 0` | 2m |
| BotErrorSpike | errors rate > 0.5/s | 5m |
| BotUpdatesStalled | 0 апдейтов за 30m при живом процессе | 10m |
| BotHighHandlerLatency | p95 > 5s | 10m |

## Валидация
```
promtool check config prometheus/prometheus.yml
promtool check rules prometheus/alerts.yml
docker compose config -q   # требует MINIO_* из .env
```
CI: compose config, promtool, yamllint.

## Порты
Все слушают ТОЛЬКО 127.0.0.1: 9090 (prom), 9093/9094 (am), 3000 (grafana), 9000/9001 (minio),
3100 (loki), 3200 (tempo), 4319/4320 (otel-collector OTLP). Наружу — ничего.

## Reverse-proxy / Telegram webhook (`reverse-proxy/`)
TLS-терминация (nginx) + Let's Encrypt (certbot, DNS-01 через duckdns).
Домен: `ninelegsbots.duckdns.org -> 2.27.204.95`.

- Сертификат выпускается по DNS-01 (duckdns TXT API) — не требует открытого
  порта 80 (провайдер play2go его фильтрует).
- **Доставка webhook от Telegram требует открытого 443** — открыть в панели/тикете
  play2go перед деплоем.
- `nginx` проксирует `/webhook/<bot>` -> `127.0.0.1:808x` (порт см. prometheus.yml).
- Боты должны быть переведены polling -> webhook (P9) и отдавать `/webhook`.

Деплой (после открытия 443):
```
cd reverse-proxy
cp .env.example .env   # DUCKDNS_TOKEN, CERTBOT_EMAIL
docker compose up -d
```

## E2E-раннер (`ops/e2e/`)
Прогоняет девять ботов **с пользовательского аккаунта**, а не токеном бота.
Индекс: `ops/e2e/PLAN.md`.

Ключевое свойство: **раннер не умеет читать токены ботов.** Юзернеймы лежат в
`ops/e2e/bots.yml` (решаются один раз через `tools/resolve_bots.py` на
админской машине, потом генератор удаляется). Гейт на это —
`tests/test_no_token_reads.py`: он запрещает и `import requests`, и любую функцию
из denylist, и литерал credentials, и неполное покрытие флота.

Авторизация проверяется явно: `connect()` бросает `RuntimeError`, если сессия не
авторизована и нет телефона. Без этой проверки прогон доходил бы до первого
сценария и там истёк по таймауту — неотличимо от «бот сломан».

Юниты в `ops/e2e/systemd/` — это контракт, а не конфигурация: `User=botkit-e2e`
без группы docker, `InaccessiblePaths=/home /root /srv /etc/botkit`,
`ReadWritePaths` только на сессию и статусы, `ExecStart` на канон в
`/opt/botkit-e2e`. Каждая директива закреплена в `tests/test_e2e_schedule.py` —
по той же причине, что и `test_smoke_schedule.py`: `systemd-analyze verify` на
живой машине всегда красный и учит игнорировать красное.

Таймер: раз в 6 часов (1 сообщение на бота за прогон, 36 в сутки).

Платёжные сценарии запрещены машинно — `tests/test_no_payment_scenarios.py`.

Проверки: `ruff check .` + `PYTHONPATH=. pytest -q` в `ops/e2e`, плюс gitleaks
по всей истории (`--baseline-path .gitleaks-baseline.json`).

## Development process

Проект создан в AI-native процессе разработки: код производили AI coding-агенты
в настроенном мной agent harness — то есть по слотам требований, инструкций, ограничений
и критериев приёмки, которые я задал заранее.

Моя роль в проекте:

- продуктовая постановка и пользовательские сценарии;
- декомпозиция задачи на самостоятельные инженерные этапы;
- context engineering: инструкции, ограничения и рабочие правила для агентов;
- управление контекстным окном между итерациями;
- цикл «спецификация → генерация → запуск → проверка → исправление»;
- валидация результата, тестирование, ревью;
- контроль структуры репозитория, конфигурации, документации и воспроизводимости запуска.

Implementation code was generated with AI coding agents under human-led engineering control.

Полное описание процесса, шаблон `AGENTS.md` и чек-листы ревью AI-кода и секретов —
в репозитории [agentic-development-playbook](https://github.com/ninelegsdog).
