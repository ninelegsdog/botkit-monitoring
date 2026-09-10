# Резервное копирование и восстановление botkit-ботов

Скрипты находятся в `ops/backup/`, развёрнуты на проде в `/root/`.

## Что бэкапится
- `data/bot.db` (SQLite) каждого бота → `/home/deploy/<bot>/backups/bot.db.<TS>`
- `dump.rdb` (Redis) каждого бота → `/home/deploy/<bot>/backups/redis.rdb.<TS>`
- Хранение: 14 копий (ротация по дате).
- Запуск: systemd `backup-bots.timer` каждые 6ч.

## Целостность
После каждого бэкапа `backup_bots.sh` проверяет:
- SQLite: `PRAGMA integrity_check` == `ok`
- Redis: магия файла `REDIS` и размер > 0

Результат пишется в `/var/backups/botkit/status/<bot>.ok` (успех) или `.fail`.
Если любой бот упал — сервис завершается с кодом 1.

## Проверка свежести + алерт
`check_backups.sh` (timer `check-backs.timer`, каждый час):
- нет `.ok` старше 7ч, или есть `.fail` → алерт в Alertmanager (`localhost:9093`)
- алерты троттлятся: повтор не чаще 1 раза в 6ч на бота
- выход с кодом 1 при проблемах (видно в `systemctl status check-backs`)

## Проверка восстановления (не разрушающая)
```bash
/root/restore_test.sh botkit-bookingbot
```
Проверяет целостность sqlite и загружаемость redis RDB во временный контейнер.
Ничего не меняет в работающих ботах.

## Восстановление (разрушающее)
```bash
# 1. сначала убедиться, что бэкап валиден:
/root/restore_test.sh botkit-bookingbot
# 2. сделать свежий бэкап перед восстановлением (на всякий случай):
/root/backup_bots.sh
# 3. восстановить:
FORCE=1 /root/restore_bot.sh botkit-bookingbot
```
`restore_bot.sh` останавливает bot+redis, сохраняет текущее состояние в
`backups/pre-restore.<TS>.*`, заменяет `data/bot.db` и redis dump, запускает обратно.

## Деплой изменений
```bash
scp ops/backup/*.sh root@2.27.204.95:/root/
scp ops/backup/systemd/check-backs.* root@2.27.204.95:/etc/systemd/system/
ssh root@2.27.204.95 'systemctl daemon-reload && systemctl enable --now check-backs.timer'
```

## АКТУАЛЬНАЯ архитектура (fix(backup), 2026-09)
- sqlite: бэкап живёт в cron-контейнере botkit-backup-cron (bash /run_pullbackups.sh ->
  docker run runner). Деплой-скрипт: /home/deploy/pullbackups.sh (копия в репо
  ops/backup/pullbackups.sh). Механика: в-контейнерный python выполняет ONLINE BACKUP API
  (src.backup(dst), uri mode=ro) в /app/backups/export.<TS>.db -> export в
  /home/deploy/backups-export/<bot>/export.<TS>.db (keep 40) -> pushoffsite.sh копирует в
  приватный GH-репо (offsite-копия, 1h после).
- redis: отдельно root-скриптом backup_bots.sh (systemd backup-bots.timer, раз в 6ч):
  bgsave через shared redis (6380) -> /home/deploy/botkit-shared-redis/backups/redis.rdb.<TS>.
- статусы: /var/backups/botkit/status/<bot>.ok|.fail, таймер check-backs.timer раз в час
  отправляет скрипт на проверку.
- ВАЖНО: docker cp локальную часть пути резолвит В FS КЛИЕНТА (где работает CLI), НЕ
  на docker-демоне. Изнутри cron-контейнера путь должен существовать там же; при патче
  на хосте (docker cp ... /home/deploy/... ) это корректно. Не запускать docker cp из
  контейнера с путём, который есть только на хосте через bind => та же трапка.
- e2e-проверка: ops/e2e/backup-restore-e2e.sh (свежесть export + PRAGMA integrity_check
  + rdb). Прогонять после правок схемы бэкапа.
