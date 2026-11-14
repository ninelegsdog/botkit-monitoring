# Runbook: BotkitBackupVerifyStale / BotkitBackupVerifyFailed

## Симптомы
- `BotkitBackupVerifyStale` — метрика `botkit_backup_verify_last_run_seconds` старше 100 дней (квартал + запас).
- `BotkitBackupVerifyFailed` — метрика `botkit_backup_verify_ok == 0` (глубокая проверка не прошла).

## Диагностика
1. `systemctl status botkit-restic-verify.timer` — таймер активен (квартальный, 1-е число 01/04/07/10 в 03:00 UTC).
2. `journalctl -u botkit-restic-verify` — ошибки последнего прогона.
3. `ls -l /var/lib/node-exporter-textfile/botkit_backup_verify.prom` — файл существует, права 644.
4. `cat /var/log/botkit-restic-verify.log` — подробный вывод проверки.

## Лечение
- Если таймер пропустил прогон: проверить системное время и включить таймер.
- Если метрика отсутствует совсем: юнит падает до записи — проверить ReadWritePaths.
- Если проверка не прошла (ok=0): данные не читаются или не сходятся — проверить пароли в `PW_DIR` и доступ к репозиториям restic.
- Первый запуск: метрика появится только после первого прогона, это ожидаемо.

## Ожидаемый результат
Метрика `botkit_backup_verify_last_run_seconds` свежая, `botkit_backup_verify_ok == 1`, алерт гаснет.
