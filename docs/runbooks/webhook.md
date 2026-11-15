# Runbook: BotWebhookDown / BotWebhookCheckStale / BotWebhookLatencySLOBreach / BotWebhookTLSUntrusted

## Симптомы
- `BotWebhookDown` — публичный вебхук бота не отвечает 3+ минут.
- `BotWebhookCheckStale` — метрика `botkit_webhook_check_timestamp` не обновляется более 30 минут.
- `BotWebhookLatencySLOBreach` — p95 латентности вебхука > 3с в течение 10 минут.
- `BotWebhookTLSUntrusted` — публичный сертификат не проходит проверку доверия.

## Диагностика
1. `systemctl status botkit-webhook-check.timer` — таймер активен (каждые 15 мин).
2. `journalctl -u botkit-webhook-check` — ошибки последней проверки доставки.
3. `curl -I https://ninelegsbots.duckdns.org/webhook/<bot>` — вебхук отвечает?
4. `ls -l /var/lib/node-exporter-textfile/botkit_webhook_run.prom` — метрика жизни проверки.
5. Для TLS: проверить цепочку сертификатов и has_custom_certificate в Telegram.

## Лечение
- Если таймер выключен: `systemctl enable --now botkit-webhook-check.timer`.
- Если вебхук не отвечает: проверить nginx, бота, сетевое подключение.
- Если TLS не trusted: обновить сертификат, проверить корневые CA.

## Ожидаемый результат
Вебхук отвечает, проверка доставки работает, метрика `botkit_webhook_check_timestamp` свежая.
