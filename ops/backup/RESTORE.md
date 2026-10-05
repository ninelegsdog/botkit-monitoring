# Резервное копирование и восстановление botkit-ботов

Скрипты находятся в `ops/backup/`, развёрнуты на проде в `/root/`.

## Что бэкапится
- `data/bot.db` (SQLite) каждого бота → `/home/deploy/<bot>/backups/bot.db.<TS>`
- `dump.rdb` — **один общий** Redis на всех девять ботов
  (контейнер `botkit-shared-redis-redis-1`) →
  `/home/deploy/botkit-shared-redis/backups/redis.rdb.<TS>`
- Хранение: 14 копий (ротация по дате).
- Запуск: systemd `backup-bots.timer` каждые 6ч.

**История перехода.** До 02.09.2026 у каждого бота был свой сервис Redis, и дампы лежали
в `/home/deploy/<bot>/backups/redis.rdb.<TS>`. С 02.09 Redis общий; старые файлы ещё
лежали в каталогах ботов до 30.09.2026, когда `backup_bots.sh` начал их удалять. Если
где-то ещё встретится `/home/deploy/<bot>/backups/redis.rdb.*` — это мёртвый остаток,
восстанавливать из него нельзя.

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
# 1. сначала убедиться, что бэкап валиден (ничего не меняет):
/root/restore_test.sh botkit-bookingbot
# 2. посмотреть, что вообще будет восстановлено (тоже ничего не меняет):
/root/restore_bot.sh botkit-bookingbot
# 3. восстановить только SQLite бота:
FORCE=1 /root/restore_bot.sh botkit-bookingbot
# 4. восстановить ещё и общий Redis — ЗАТРАГИВАЕТ ВСЕ 9 БОТОВ:
FORCE=1 FORCE_REDIS=1 /root/restore_bot.sh botkit-bookingbot
```

Почему Redis отдельным флагом: он общий, и его восстановление затирает живое состояние
всех девяти ботов. Молча восстанавливать его вместе с базой одного бота нельзя — это
уже было ошибкой в прежней версии скрипта вместе с поиском дампа в каталоге бота.

Что делает `restore_bot.sh`:
- без `FORCE=1` — только dry-run, ничего не меняет;
- **до** остановки бота проверяет `PRAGMA integrity_check` выбранного бэкапа и его
  возраст (старше 24ч — отказ, обойти через `FORCE_STALE=1`);
- сохраняет текущее состояние в `backups/pre-restore.<TS>.*`;
- заменяет `data/bot.db`, поднимает сервис;
- redis трогает только при `FORCE_REDIS=1`, с собственным отказом по устареванию;
- при любой ошибке поднимает бота обратно и выходит с ненулевым кодом.

## Деплой изменений
```bash
scp ops/backup/*.sh root@2.27.204.95:/root/
scp ops/backup/systemd/check-backs.* root@2.27.204.95:/etc/systemd/system/
ssh root@2.27.204.95 'systemctl daemon-reload && systemctl enable --now check-backs.timer'
```

## Offsite-бэкап: restic вместо git (2026-09-27)

Git-based offsite (`ninelegsdog/botkit-backups-offsite`) удалён. Причины, а не вкус:
у git нет удаления снапшотов, нет шифрования и нет retention, а токен лежал на проде
в `/home/deploy/.git-token` и **не попал в ротацию инцидента 335** — 15 дней отказа
никто не заметил, потому что свежесть offsite никто не проверял. Репозиторий удалён,
токен отозван, подтверждено `HTTP 401` от `api.github.com`.

Транспорт — SFTP, не git и не публичное хранилище:

```
sftp:botkit-backup@31.76.11.198:/repo-data      # боты + секреты, пароль №1
sftp:botkit-backup@31.76.11.198:/repo-monitor   # метрики, пароль №2 (другой!)
```

На приёмнике пользователь `botkit-backup`: `ChrootDirectory /srv/botkit-backup`,
`ForceCommand internal-sftp`, `restrict` в `authorized_keys`. Сессия не может выйти
из каталога репозиториев, команда через SSH не выполняется вовсе.

| поток | содержимое | retention | расписание |
|-------|-----------|-----------|------------|
| `data` | 9 ботов (online-backup API), локальные копии, Redis RDB, 12 `.env` | 14/8/6 | `00,06,12,18:30` |
| `monitor` | тома prometheus/grafana/alertmanager/loki/minio/tempo + конфиг | 3/2 | `01,07,13,19:00` |

Согласованность важнее скорости: `restic-backup.sh` снимает каждую БД через
`sqlite3.backup()` внутри контейнера. Обычный `cp` (как в `backup_bots.sh`) может
дать надорванную копию, если база пишется в этот момент.

## Свежесть offsite под контролем

`restic-check.sh` (`botkit-restic-check.timer`, раз в 15 мин) пишет в textfile-коллектор
`botkit_backup_age_seconds` / `botkit_backup_ok` и при проблеме шлёт алерт в Alertmanager.
Алерты в `prometheus/alerts.yml`: `BotkitBackupStale`, `BotkitBackupFailed`,
`BotkitBackupCheckMissing`, `BotkitBackupRunFailed` (часть ботов без согласованного снимка).
`check_backups.sh` (час) остался контролировать локальные копии — это разные вещи.

## Восстановление из offsite

```bash
export RESTIC_PASSWORD_FILE=/root/.botkit-backup/data.pw
R="sftp:botkit-backup@31.76.11.198:/repo-data"
restic -r "$R" snapshots
restic -r "$R" restore latest --tag data --target /tmp/drill
# обязательная проверка целостности:
python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('pragma integrity_check').fetchone()[0])" \
  /tmp/drill/var/lib/botkit-restic-stage/bookingbot/export.<TS>.db
```

`integrity_check` обязан вернуть `ok`. Дальше — точечная замена файла бота
по процедуре ниже (разрушающей части касаться только после успешной проверки).

## Пароли репозиториев

Два независимых пароля, по одному на репозиторий. Лежат только на проде:
`/root/.botkit-backup/data.pw` и `/root/.botkit-backup/monitor.pw` (600).
**Второго экземпляра паролей нет нигде — потеря прода означает потерю бэкапов.**
Шаблон параметров и процедура bootstrap: `ops/backup/restic.env.example`.

## Деплой бэкап-части

На живом хосте оба скрипта — **симлинки в клон канона**: правка подхватывается
`git pull`, второй копии не существует и ей нечем устареть. 05.10.2026 копия в
`/usr/local/sbin` отстала от канона на три дня (S1 добавил в скрипт
`source ../lib/fleet.sh`), и после переустановки она падала на несуществующем
`/usr/local/lib/fleet.sh` — таймер продолжал писать «все потоки в норме» по старой
версии, пока новая даже не запускалась.

```bash
ln -sf /home/deploy/botkit-monitoring/ops/backup/restic-backup.sh /usr/local/sbin/botkit-restic-backup
ln -sf /home/deploy/botkit-monitoring/ops/backup/restic-check.sh  /usr/local/sbin/botkit-restic-check
```

Чистая машина, где репозитория ещё нет, — копии; скрипты ищут `../lib/*` от своего
каталога, поэтому `ops/lib/` целиком ставится рядом в `/usr/local/lib/` (там
`fleet.sh` + `fleet.env` для адресов мониторинга и `snapshot.sh` — общий механизм
согласованной копии SQLite, который используют оба скрипта):

```bash
scp ops/backup/restic-backup.sh ops/backup/restic-check.sh root@2.27.204.95:/usr/local/sbin/
scp ops/lib/*                                                 root@2.27.204.95:/usr/local/lib/
scp ops/backup/systemd/botkit-restic-*           root@2.27.204.95:/etc/systemd/system/
ssh root@2.27.204.95 'chmod 750 /usr/local/sbin/botkit-restic-*; systemctl daemon-reload; \
  systemctl enable --now botkit-restic-data.timer botkit-restic-monitor.timer botkit-restic-check.timer'
```
