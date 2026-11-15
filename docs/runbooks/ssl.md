# Runbook: SSLCertExpiring / SSLCertExpired

## Симптомы
- `SSLCertExpiring` — сертификат истекает через менее 30 дней.
- `SSLCertExpired` — сертификат истёк.

## Диагностика
1. Проверка сертификата: `echo | openssl s_client -connect <host>:443 2>/dev/null | openssl x509 -noout -dates`
2. `certbot certificates` — статус сертификатов Certbot.
3. Проверка автопродления: `certbot renew --dry-run`.
4. Для вебхуков: проверить has_custom_certificate в Telegram Bot API.

## Лечение
- `certbot renew` — продлить сертификаты.
- Перезагрузить nginx после обновления сертификата.
- Для вебхуков: обновить сертификат в Telegram через setWebhook.

## Ожидаемый результат
Сертификаты валидны, не истекают в ближайшие 30 дней, алерты гаснут.
