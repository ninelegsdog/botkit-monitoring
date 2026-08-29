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
