# Runbook: BotkitSmokeStale

## Симптомы
- `BotkitSmokeStale` — метрика `botkit_smoke_last_run_seconds` не обновляется более 30 минут или отсутствует.

## Диагностика
1. `systemctl status botkit-smoke.timer` — таймер активен (каждые 30 мин).
2. `journalctl -u botkit-smoke` — ошибки последнего прогона smoke_all.sh.
3. `ls -l /var/lib/node-exporter-textfile/botkit_smoke.prom` — файл существует, права 644.
4. `/home/deploy/botkit-monitoring/ops/e2e/smoke_all.sh` — запустить вручную для проверки.

## Лечение
- Если таймер выключен: `systemctl enable --now botkit-smoke.timer`.
- Если метрика отсутствует: smoke_all.sh не пишет текстfile — проверить права на каталог textfile.
- Если проверка падает: посмотреть `/var/log/botkit-smoke.log` и 알ерт BotkitSmokeFailed в Alertmanager.

## Ожидаемый результат
Метрика `botkit_smoke_last_run_seconds` свежая, алерт гаснет.
