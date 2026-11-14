# Runbook: BotkitDriftStale / BotkitDriftDetected

## Симптомы
- `BotkitDriftStale` — метрика `botkit_drift_last_check_timestamp_seconds` не обновляется более 45 минут или отсутствует.
- `BotkitDriftDetected` — `min(botkit_drift_ok) < 1` (клон бота расходится с origin/main).

## Диагностика
1. `systemctl status botkit-drift.timer` — таймер активен (каждые 15 минут).
2. `journalctl -u botkit-drift` — ошибки, включая 'Read-only file system'.
3. `ls -l /var/lib/node-exporter-textfile/botkit_drift.prom` — файл существует, права 644.
4. `systemctl show botkit-drift -p ReadWritePaths` — каталог textfile в ReadWritePaths.
5. `/var/log/botkit-drift.log` — строки с DRIFT.

## Лечение
- Если таймер выключен: `systemctl enable --now botkit-drift.timer`.
- Если 'Read-only file system': добавить каталог textfile в ReadWritePaths юнита.
- Если дрейф обнаружен: синхронизировать клон с origin/main (`git checkout -B <branch> origin/main` в клоне).

## Ожидаемый результат
Метрика `botkit_drift_last_check_timestamp_seconds` свежая, `botkit_drift_ok == 1`, алерты гаснут.
