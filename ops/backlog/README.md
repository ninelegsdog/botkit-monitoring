# Неизменяемая реплика бэкапов — BOTKIT_BACKLOG (predator)

Канон схемы «№1» из [ADR 0002](../../docs/adr/0002-rpo-rto-backup-immutability.md):
репозитории restic с монитора (31.76.11.198, sftp) копируются на локальный диск
predator и хранятся в MinIO с S3 Object Lock, режим **COMPLIANCE**, retention
**90 дней**. Уничтожить данные в срок retention нельзя ни через restic, ни через
S3-API, ни корневым пользователем MinIO — доказано мутациями (см. ADR 0002).

Этот каталог — **источник истины** для скриптов стека. Развёрнутая копия лежит
на predator в `/mnt/storage/BOTKIT_BACKLOG/stack/`; после правки здесь файлы
переносятся на predator (`scp ops/backlog/* predator:/mnt/storage/BOTKIT_BACKLOG/stack/`)
и, для восстановления exec-бита на NTFS-fuse, запускаются через `bash <script>`.

## Раскладка на predator

- `/mnt/storage/BOTKIT_BACKLOG/minio-data/` — данные MinIO (бакет `botkit-backlog`,
  префиксы `data/`, `cp/`, `monitor/` — по одному на исходный репозиторий restic)
- `/mnt/storage/BOTKIT_BACKLOG/stack/` — `compose.yml`, `replicate.sh`,
  `fill-pws.sh`, `worm-drill.sh`, `verify.sh` (копия этого каталога)
- `~/.local/state/botkit-backlog/replicate.log` — лог репликации;
  `replicate.lock` — flock (один прогон за раз); `heartbeat.json` — состояние
  последнего прогона (его копия уезжает на монитор)

## Компоненты

- `compose.yml` — контейнер MinIO. Пин по digest (публичные образы MinIO после
  апреля 2025 недоступны — `dl.min.io` отдаёт 410; рабочий канал — архив
  Bitnami). `user: "1000:1000"` — NTFS-fuse не даёт писать uid 1001;
  `cap_drop: ALL`, `no-new-privileges`, `read_only` + tmpfs, слушает только
  `127.0.0.1:9000/9001`.
- `replicate.sh` — `restic copy` из sftp-репозиториев монитора в
  `s3:http://127.0.0.1:9000/botkit-backlog/<repo>`. Гейт хранилища (диск
  примонтирован и контейнер видит **то же устройство**, что хост-путь), `restic
  unlock` перед копированием, per-repo SKIP/PASS, heartbeat для сторожа,
  ненулевой код возврата при сбое. Cron: `17 * * * * bash /mnt/storage/BOTKIT_BACKLOG/stack/replicate.sh`.
- `heartbeat-watch.sh` — **на мониторе**: читает `heartbeat.json` реплики и
  превращает его в push в Uptime Kuma (`up`/`down`). Второй push-монитор Kuma —
  внешний стоп-кран рядом с тем, которым прод сообщает о своей живости.
- `verify.sh` — приёмка реплики на predator: замок и версии, закалка контейнера,
  совпадение счётчиков с источником, недоступность реплики паролями источника,
  залежавшиеся локи, место, свежесть heartbeat и его копия на мониторе, cron.
  Коды возврата: 0 — проверено и чисто, 1 — дефект, 2 — проверить нечем.
- `fill-pws.sh` — разовый скрипт владельца: внос паролей ИСХОДНЫХ репозиториев
  (`data`/`cp`/`monitor`) из KeePass. Требует tty, ввод скрыт, дедлайн 300 с.
- `worm-drill.sh` — дрилл неизменяемости (см. ниже).

## Секреты (`~/.config/botkit-backlog/`, chmod 600/700; владелец ведёт в KeePass)

| Файл | Что |
|---|---|
| `minio.env` | корневой пользователь MinIO (репликация и администрирование) |
| `target.pw` | пароль целевых (WORM) репозиториев restic |
| `data.pw`, `cp.pw`, `monitor.pw` | пароли ИСХОДНЫХ репозиториев (вносит владелец через `fill-pws.sh`) |

В репозиторий эти файлы не попадают (в `.gitignore` их не требуется — каталог
вне репозитория; в коммит уходят только скрипты и `compose.yml` без секретов).

## Разовая настройка (владелец)

1. `bash fill-pws.sh` — внести три пароля источников из KeePass.
2. Первый прогон: `bash replicate.sh`.
3. Занести в KeePass: `minio.env` (пароль MinIO) и `target.pw`.
4. Создать в Kuma (монитор, `http://127.0.0.1:3001`) push-монитор
   «botkit-backlog replica»: интервал 5 минут, повторы 3, канал оповещения — тот
   же, что у «botkit-prod heartbeat». Его push-URL положить на монитор в
   `/root/.botkit-backlog/kuma-push.url` (**chmod 600**). Пока файла нет, сторож
   пишет `FAIL ... push-URL Kuma не заведён` и код 1: тревога не поднята, но и не
   подделана.

## Старт после ребута: порядок монтирования (P0-2)

`docker.service` на этой Ubuntu не имеет `After=local-fs.target`, а раздел в
fstab помечен `nofail`: система штатно стартует и без NTFS. Тогда bind-mount
создаёт каталог на **корневом** разделе, MinIO поднимает пустое хранилище, и
реплика молча пропадает — контейнер здоров, порты слушают, данные лежат не там.
Поэтому жизненным циклом контейнера управляет user-юнит, а не политика docker:

```sh
scp ops/backlog/systemd/botkit-backlog-stack*.service \
    ops/backlog/systemd/botkit-backlog-stack-retry.timer \
    predator:~/.config/systemd/user/
ssh predator 'systemctl --user daemon-reload && \
  systemctl --user enable --now botkit-backlog-stack.service botkit-backlog-stack-retry.timer'
```

- `botkit-backlog-stack.service` — `RequiresMountsFor=/mnt/storage` плюс ожидание
  монтирования до 5 минут; при снятом диске юнит падает **и остаётся упавшим**;
- `botkit-backlog-stack-retry.timer` — раз в 15 минут повторяет `start`, поэтому
  поздно появившийся диск лечится сам;
- `compose.yml` держит `restart: "no"`: docker больше не поднимает контейнер
  раньше монтирования.

Linger для `algtro` на predator включён, поэтому user-юниты стартуют с загрузкой.

## Наблюдаемость: heartbeat и сторож

Молчание — не доказательство: прогон, который не состоялся, выглядит как
успешный, а cron без MTA теряет stderr. Поэтому каждый прогон `replicate.sh`
заканчивается `heartbeat.json` (время, ok, причина, состояние монтирования,
счётчики снапшотов, объём бакета, свободное место) и загружает копию по sftp на
монитор. Сторож на мониторе раз в 5 минут читает её и push-ит состояние в Kuma:

| Состояние реплики | Вердикт сторожа |
|---|---|
| свежий heartbeat, `ok=1`, диск верный | `up` |
| heartbeat старше 3 часов (пропущено ≥3 прогона подряд) | `down` («устарел») |
| `ok=0` или `mount_ok`/`device_ok` = 0 | `down` |
| файла нет / нечитаем / нет `ts` | `down` |

Сторож живёт **на мониторе**, а не на predator: наблюдатель, стоящий рядом с
наблюдаемым, падает вместе с ним. Он отправляет `down` явно, а Kuma отдельно
замечает пропавшие push-и — то есть убийство продюсера (или самого predator)
тоже становится тревогой.

Установка сторожа (root на мониторе):

```sh
scp ops/backlog/heartbeat-watch.sh root@31.76.11.198:/usr/local/sbin/botkit-backlog-heartbeat-watch
scp ops/backlog/systemd/botkit-backlog-heartbeat.service \
    ops/backlog/systemd/botkit-backlog-heartbeat.timer \
    root@31.76.11.198:/etc/systemd/system/
ssh root@31.76.11.198 'chmod 750 /usr/local/sbin/botkit-backlog-heartbeat-watch && \
  install -d -o botkit-backup -g botkit-backup -m 700 /srv/botkit-backup/heartbeat && \
  systemctl daemon-reload && systemctl enable --now botkit-backlog-heartbeat.timer'
```

`/srv/botkit-backup/heartbeat` — каталог внутри chroot sftp-аккаунта: predator
кладёт туда копию heartbeat своим ключом (`replicator@predator`), больше ничего
этот ключ не открывает. Журнал сторожа — `/var/log/botkit-backlog-heartbeat.log`.

## Приёмка: `verify.sh`

```sh
bash /mnt/storage/BOTKIT_BACKLOG/stack/verify.sh          # быстро
bash /mnt/storage/BOTKIT_BACKLOG/stack/verify.sh --full   # + check --read-data (квартально)
```

Скрипт падает кодом 1 при любом дефекте и кодом 2, если проверить нечем
(например, нет venv с boto3) — «нечем проверить» это не «всё хорошо».

## Восстановление из BOTKIT_BACKLOG

```sh
. ~/.config/botkit-backlog/minio.env
export AWS_ACCESS_KEY_ID=$MINIO_ROOT_USER AWS_SECRET_ACCESS_KEY=$MINIO_ROOT_PASSWORD
export AWS_DEFAULT_REGION=us-east-1
export RESTIC_PASSWORD_FILE=~/.config/botkit-backlog/target.pw
~/.local/bin/restic -r s3:http://127.0.0.1:9000/botkit-backlog/<repo> snapshots
~/.local/bin/restic -r s3:http://127.0.0.1:9000/botkit-backlog/<repo> restore latest --target /tmp/restored
```

## Проверка неизменяемости: `worm-drill.sh`

Дрилл модерирует атаку и её обратимость на **боевой** реплике. Что делает:

1. считает снапшоты и запоминает самый старый;
2. запускает `restic forget --keep-last 1 --prune --group-by ""` (атака
   «стереть всю историю»); `--group-by ""` делает её максимально сильной —
   видимым остаётся ровно один снапшот;
3. проверяет, что data-версии целы, а удаления — только delete-маркеры;
4. проверяет, что спрятанный снапшот не восстанавливается (эффект атаки реален);
5. снимает маркеры (кроме `/locks/`) и проверяет, что вся история вернулась и
   restore снова работает.

Запускать вручную (квартально) при остановленной репликации:
`bash worm-drill.sh [data|cp|monitor]`. Код возврата 0 — PASS.
Результат 18.10.2026: PASS на `data` (30→1→30, 117 маркеров) и `monitor`
(5→1→5, 25 маркеров).

**Грабли, на которые уже наступали** (обе заложены в скрипт):

- снятие delete-маркера **возвращает** объект видимым, а не удаляет его: маркеры
  под `/locks/` снимать нельзя, иначе «оживают» старые локи;
- «зависший» эксклюзивный лок блокирует все чтения; лечится `restic unlock --remove-all`
  (прямое удаление объектов локов через S3-API лишь создаёт маркеры, а их снятие
  возвращает локи обратно).

## Свойства и ограничения (честно)

- Целевые репозитории **не чистятся** (`forget`/`prune` в штатном режиме не
  запускаются): место занимается, не освобождается; возврат места — только после
  истечения 90 дней и снятия delete-маркеров.
- Удаление без `VersionId` в versioned-бакете лишь **прячет** объект маркером;
  данные остаются и возвращаются снятием маркера — штатная семантика S3 Object Lock.
- Угроза, от которой защита **не** спасает: компрометация самого predator (root
  стирает файлы с диска напрямую, мимо MinIO). Защита действует против атак на
  мониторе/проде: там нет ни паролей WORM-репозиториев, ни доступа к MinIO
  (только 127.0.0.1 на predator, вне VPN-сети монитора).
- Границу ослабляет сам predator: в его `~/.ssh` лежат ключи `vps-root` (root на
  мониторе) и `botkit-prod-root`. Атакующий на predator получает не только
  реплику, но и монитор с продом. Пока это осознанный компромисс удобства; если
  граница важнее — ключи переносятся на отдельный административный носитель.
