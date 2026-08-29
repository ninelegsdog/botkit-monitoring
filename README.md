# botkit-monitoring

Мониторинг 8 ботов BotKit: Prometheus + Alertmanager + Grafana (host-network, только 127.0.0.1).

## Стек
- **Prometheus** `127.0.0.1:9090` — скрайпит `/metrics` ботов на портах 8081–8088
- **Alertmanager** `127.0.0.1:9093` — алерты → Telegram (`telegram_configs`)
- **Grafana** `127.0.0.1:3000` — дашборд «BotKit Overview» (переменная instance), доступ через SSH-туннель

## Деплой на VPS
```
cd /opt/monitoring
git pull
cp .env.example .env  # TG_BOT_TOKEN, TG_CHAT_ID, GRAFANA_ADMIN_PASSWORD
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
```
CI: compose config, promtool, yamllint.

## Порты
Все слушают ТОЛЬКО 127.0.0.1: 9090 (prom), 9093/9094 (am), 3000 (grafana). Наружу — ничего.

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

