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
  `fill-pws.sh`, `worm-drill.sh` (копия этого каталога)
- `~/.local/state/botkit-backlog/replicate.log` — лог репликации;
  `replicate.lock` — flock (один прогон за раз)

## Компоненты

- `compose.yml` — контейнер MinIO. Пин по digest (публичные образы MinIO после
  апреля 2025 недоступны — `dl.min.io` отдаёт 410; рабочий канал — архив
  Bitnami). `user: "1000:1000"` — NTFS-fuse не даёт писать uid 1001;
  `cap_drop: ALL`, `no-new-privileges`, `read_only` + tmpfs, слушает только
  `127.0.0.1:9000/9001`.
- `replicate.sh` — `restic copy` из sftp-репозиториев монитора в
  `s3:http://127.0.0.1:9000/botkit-backlog/<repo>`. flock, per-repo SKIP/PASS,
  ненулевой код возврата при сбое. Cron: `17 * * * * bash /mnt/storage/BOTKIT_BACKLOG/stack/replicate.sh`.
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
