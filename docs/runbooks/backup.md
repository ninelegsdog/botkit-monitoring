# Runbook: BotkitBackupStale / BotkitBackupFailed / BotkitBackupCheckMissing / BotkitBackupRunFailed / BotkitBackupRunMetricMissing

## Симптомы
- `BotkitBackupStale` — бэкап потока старше 8 часов (botkit_backup_age_seconds > 28800).
- `BotkitBackupFailed` — проверка бэкапа не проходит (botkit_backup_ok == 0).
- `BotkitBackupCheckMissing` — метрики проверки бэкапа нет (absent(botkit_backup_ok)).
- `BotkitBackupRunFailed` — боты без согласованного снимка БД (max_over_time(botkit_backup_consistent_failures[15m]) > 0).
- `BotkitBackupRunMetricMissing` — метрики запуска бэкапа нет (absent(botkit_backup_consistent_failures)).

## Диагностика
1. `systemctl status botkit-restic-check.timer` — таймер активен (каждые 6 часов).
2. `journalctl -u botkit-restic-check` — ошибки последнего прогона.
3. `ls -l /var/lib/node-exporter-textfile/botkit_backup.prom` — файл существует, права 644.
4. `docker system df` — место на диске.
5. Проверка доступа к SFTP-репозиторию restic на мониторе.

## Лечение
- Если таймер выключен: `systemctl enable --now botkit-restic-check.timer`.
- Если репозиторий недоступен: проверить SFTP, пароли, сетевое подключение к монитору.
- Если БД не удаётся снять: проверить sqlite online-backup API, права на файлы БД.

## Ожидаемый результат
Бэкапы делаются регулярно, метрики свежие, алерты гаснут.
