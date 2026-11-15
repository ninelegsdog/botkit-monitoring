# Runbook: BotkitHealthcheckStale / BotkitHealthcheckFailed

## Симптомы
- `BotkitHealthcheckStale` — метрика `botkit_healthcheck_last_run_seconds` не обновляется более 15 минут или отсутствует.
- `BotkitHealthcheckFailed` — метрика `botkit_healthcheck_ok == 0` (проверка не прошла).

## Диагностика
1. `systemctl status botkit-healthcheck.timer` — таймер активен?
2. `journalctl -u botkit-healthcheck` — ошибки последнего прогона.
3. `ls -l /var/lib/node-exporter-textfile/botkit_healthcheck.prom` — файл существует, права 644, время модификации свежее.
4. `systemctl show botkit-healthcheck -p ReadWritePaths` — каталог textfile в ReadWritePaths.

## Лечение
- Если таймер выключен: `systemctl enable --now botkit-healthcheck.timer`.
- Если юнит падает до записи метрики: проверить ReadWritePaths и права на каталог.
- Если метрика отсутствует совсем: прогнать `botkit-healthcheck.service` вручную и проверить вывод.

## Ожидаемый результат
Метрика `botkit_healthcheck_last_run_seconds` в textfile, `botkit_healthcheck_ok == 1`, алерт гаснет.
