# Runbook: PrometheusDown / AlertmanagerDown / AlertmanagerNotificationFailures

## Симптомы
- `PrometheusDown` — Prometheus (127.0.0.1:9090) не отвечает 2+ минут.
- `AlertmanagerDown` — Alertmanager (127.0.0.1:9093) не отвечает 2+ минут.
- `AlertmanagerNotificationFailures` — неудачные попытки доставки уведомлений за 10 минут.

## Диагностика
1. `docker ps | grep monitoring` — контейнеры мониторинга запущены?
2. `docker logs botkit-monitoring-prometheus-1` — ошибки Prometheus.
3. `docker logs botkit-monitoring-alertmanager-1` — ошибки Alertmanager.
4. `curl http://127.0.0.1:9090/api/v1/status` — состояние Prometheus.
5. `curl http://127.0.0.1:9093/api/v2/status` — состояние Alertmanager.
6. Проверка SIGHUP после изменения правил: `docker kill -s HUP botkit-monitoring-prometheus-1`.

## Лечение
- Если контейнер упал: `docker restart <имя>`.
- Если правила не загрузились: SIGHUP на Prometheus, проверить `promtool check rules`.
- Если уведомления не доставляются: проверить настройки Alertmanager (TG bot token, chat IDs).

## Ожидаемый результат
Prometheus и Alertmanager отвечают, уведомления доставляются, алерты гаснут.
