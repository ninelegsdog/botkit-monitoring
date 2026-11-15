# Runbook: BotkitRolloutCheckStale / BotkitRolloutCheckFailed

## Симптомы
- `BotkitRolloutCheckStale` — метрика `botkit_rollout_check_last_run_seconds` не обновляется более 15 минут.
- `BotkitRolloutCheckFailed` — метрика `botkit_rollout_check_state > 0` (код выхода последнего прогона не ноль).

## Диагностика
1. `systemctl status botkit-rollout-check.timer` — таймер активен.
2. `journalctl -u botkit-rollout-check` — ошибки последнего прогона (код выхода, digest, registry).
3. `ls -l /var/lib/node-exporter-textfile/botkit_rollout_check.prom` — файл существует, права 644.
4. `cat /var/log/botkit-rollout.log` — подробный вывод проверки раскатки.

## Лечение
- Если таймер выключен: `systemctl enable --now botkit-rollout-check.timer`.
- Если код выхода не ноль: бот не раскатан, нет digest или отказал registry. Проверить образы и зависимости.
- Если метрика отсутствует: юнит падает до записи — проверить ReadWritePaths.

## Ожидаемый результат
`botkit_rollout_check_state == 0`, метрика `botkit_rollout_check_last_run_seconds` свежая, алерт гаснет.
