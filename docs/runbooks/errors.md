# Runbook: BotErrorSpike / BotErrorBudgetBurn* / BotMetricsUnavailable / BotHighHandlerLatency / BotHandlerLatencySLOBreach

## Симптомы
- `BotErrorSpike` — больше 0.5 ошибок/сек в течение 5 минут.
- `BotErrorBudgetBurnFast/Medium/Slow` — отношение ошибок к апдейтам превышает порог.
- `BotMetricsUnavailable` — виден трафик (updates_total), но botkit_errors_total отсутствует.
- `BotHighHandlerLatency` / `BotHandlerLatencySLOBreach` — p95 латентности хэндлера превышает порог.

## Диагностика
1. `journalctl -u botkit-<имя>` — ошибки в логах конкретного бота.
2. `curl http://<bot>:<port>/metrics` — проверить presence botkit_errors_total и botkit_updates_total.
3. Если botkit_errors_total отсутствует совсем — бот не инициализировал счётчик ошибок (нет ни одной ошибки или бот только запустился).
4. Для BotMetricsUnavailable: убедиться, что ошибки инициализируются нулями при старте (D2).

## Лечение
- Исправить причину ошибок в коде бота (network, retry_after, unhandled, domain).
- Если метрики ошибок не появляются — добавить нулевую инициализацию всех error_type при старте.

## Ожидаемый результат
Счётчик ошибок работает, алерты на бюджет ошибок корректно срабатывают и гаснут.
