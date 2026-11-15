# Runbook: BotDown / BotCrmOutboxPendingHigh / BotCrmOutboxPendingCritical

## Симптомы
- `BotDown` — /metrics бота недоступен 2+ минут.
- `BotCrmOutboxPendingHigh` — более 100 непереданных лидов в CRM.
- `BotCrmOutboxPendingCritical` — более 500 непереданных лидов в CRM.

## Диагностика
1. `docker ps | grep botkit-<имя>` — контейнер запущен?
2. `curl http://<bot>:<port>/metrics` — метрики доступны?
3. `docker logs botkit-<имя>` — ошибки в логах бота.
4. Проверка подключения к CRM API из контейнера бота.

## Лечение
- Если контейнер упал: `docker restart botkit-<имя>`.
- Если сеть недоступна: проверить DNS,ファイрволл, маршрутизацию.
- Если CRM не принимает лиды: проверить API-ключи, квоты, статус CRM.

## Ожидаемый результат
Бот отвечает на /metrics, листы передаются в CRM, алерты гаснут.
