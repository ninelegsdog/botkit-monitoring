#!/usr/bin/env bash
# Репликация restic-репозиториев монитора в WORM-хранилище predator (MinIO, COMPLIANCE 90d)
# и heartbeat для сторожа на мониторе.
#
# Зачем heartbeat, а не только лог: скрипт живёт на predator, а сторож — на мониторе,
# единственном хосте, который не падает вместе с площадкой. Прогон, упавший молча,
# оставляет реплику стухшей, и об этом никто не узнаёт: cron без MTA теряет stderr,
# а в лог не смотрит никто. Поэтому каждый прогон заканчивается файлом состояния,
# который сторож читает и превращает в push в Uptime Kuma (ops/backlog/heartbeat-watch.sh).
# Молчание сторожа — тоже сигнал.
set -euo pipefail

SECRETS=${SECRETS:-$HOME/.config/botkit-backlog}
R=${RESTIC:-$HOME/.local/bin/restic}
S3=${S3:-s3:http://127.0.0.1:9000/botkit-backlog}
STATEDIR=${STATEDIR:-$HOME/.local/state/botkit-backlog}
LOG=$STATEDIR/replicate.log
HB=$STATEDIR/heartbeat.json
MOUNT=${MOUNT:-/mnt/storage}
DATA=${DATA:-/mnt/storage/BOTKIT_BACKLOG/minio-data}
BUCKET_DIR=$DATA/botkit-backlog
CONTAINER=${CONTAINER:-botkit-backlog-minio}
SRC_ALIAS=${SRC_ALIAS:-botkit-backlog-src}
HB_REMOTE=${HB_REMOTE:-/heartbeat/botkit-backlog.json}
REPOS=${REPOS:-"data cp monitor"}

mkdir -p "$STATEDIR"
ts() { date -u +%FT%TZ; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }
# число снапшотов в репозитории (JSON, а не число строк вывода)
count_snaps() {
  "$R" -r "$1" snapshots --json 2>/dev/null \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "?"
}

# один прогон за раз
exec 9>"$STATEDIR/replicate.lock"
flock -n 9 || { log "УЖЕ ВЫПОЛНЯЕТСЯ — пропуск"; exit 75; }

# shellcheck disable=SC1090  # путь к секретам задаётся вне репозитория
set -a; . "$SECRETS/minio.env"; set +a
export AWS_ACCESS_KEY_ID="$MINIO_ROOT_USER" AWS_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD"
export AWS_DEFAULT_REGION=us-east-1
export RESTIC_PASSWORD_FILE="$SECRETS/target.pw"

started=$(date +%s)
rc_total=0
mount_ok=0
device_ok=0
reason=""
declare -A SNAPS=()

write_hb() {  # write_hb <ok> <reason>
  local hb_ok=$1 hb_reason=$2
  HB_OK="$hb_ok" HB_REASON="$hb_reason" HB_MOUNT_OK="$mount_ok" HB_DEVICE_OK="$device_ok" \
  HB_DUR=$(( $(date +%s) - started )) HB_FREE="$(df --output=avail -B1 "$MOUNT" 2>/dev/null | tail -n1 || echo 0)" \
  HB_BYTES="$(du -sb "$BUCKET_DIR" 2>/dev/null | cut -f1 || echo 0)" HB_HOST="$(hostname)" \
  HB_SNAPS="$(
    sep=""
    for r in $REPOS; do
      printf '%s%s=%s' "$sep" "$r" "${SNAPS[$r]:-0}"
      sep=" "
    done
  )" python3 - "$HB" <<'PY'
import json, os, sys, datetime
# Счётчик может вернуть "?" — тогда это строка, а не число. Собираем по парам
# key=value и приводим тип здесь: склеивание строк в JSON дало бы невалидный
# документ, а write_hb под set -e упал бы, не оставив heartbeat вовсе.
snaps = {}
for pair in os.environ["HB_SNAPS"].split():
    key, _, val = pair.partition("=")
    snaps[key] = int(val) if val.isdigit() else val
hb = {
    "ts": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
          .isoformat().replace("+00:00", "Z"),
    "host": os.environ["HB_HOST"],
    "ok": int(os.environ["HB_OK"]),
    "reason": os.environ["HB_REASON"],
    "mount_ok": int(os.environ["HB_MOUNT_OK"]),
    "device_ok": int(os.environ["HB_DEVICE_OK"]),
    "duration_s": int(os.environ["HB_DUR"] or 0),
    "disk_free_bytes": int(os.environ["HB_FREE"] or 0),
    "bucket_bytes": int(os.environ["HB_BYTES"] or 0),
    "snapshots": snaps,
}
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(hb, f, ensure_ascii=False, sort_keys=True)
    f.write("\n")
os.replace(tmp, sys.argv[1])
PY
}

push_hb() {
  if printf 'put %s %s\n' "$HB" "$HB_REMOTE" \
      | sftp -o BatchMode=yes -o ConnectTimeout=20 -b - "$SRC_ALIAS" >>"$LOG" 2>&1; then
    log "heartbeat → монитор ($HB_REMOTE): ok"
    return 0
  fi
  log "HEARTBEAT UPLOAD FAILED: сторож на мониторе не увидит свежести реплики"
  return 1
}

# Гейт хранилища. Без него отказ диска не виден: docker создаёт каталог на корневом
# разделе, и MinIO поднимает ПУСТОЕ хранилище — реплика выглядит живой, а данные
# лежат не там. Сверка device закрывает вторую половину случая: контейнер мог
# стартовать до монтирования и держать dentry корневого раздела (uid/права при этом
# выглядят нормально). Совпадать должны не строки путей, а устройства.
if mountpoint -q "$MOUNT"; then
  mount_ok=1
else
  reason="$MOUNT не примонтирован (nofail: система стартует и без него)"
  log "FATAL: $reason — репликация остановлена, чтобы не писать в корневой раздел"
fi

if [ "$mount_ok" = 1 ]; then
  host_dev=$(stat -c %d "$DATA" 2>/dev/null || echo "")
  ctr_dev=$(docker exec "$CONTAINER" bash -c 'stat -c %d /data' 2>/dev/null | tr -d '\r' || echo "")
  if [ -n "$host_dev" ] && [ "$host_dev" = "$ctr_dev" ]; then
    device_ok=1
  else
    reason="контейнер $CONTAINER видит другое устройство, чем $DATA (host=${host_dev:-нет} контейнер=${ctr_dev:-нет})"
    log "FATAL: $reason — MinIO пишет не в примонтированный каталог; репликация остановлена"
  fi
fi

if [ "$mount_ok" != 1 ] || [ "$device_ok" != 1 ]; then
  write_hb 0 "$reason"
  push_hb || true
  exit 1
fi

# shellcheck disable=SC2086  # REPOS — список репозиториев, разбиение по словам намеренное
for repo in $REPOS; do
  SRC="sftp:$SRC_ALIAS:/repo-$repo"
  TGT="$S3/$repo"
  srcpw="$SECRETS/$repo.pw"
  if [ ! -s "$srcpw" ]; then
    log "SKIP $repo: нет пароля источника ($srcpw)"
    rc_total=1
    continue
  fi
  log "=== $repo: copy ==="
  if ! "$R" -r "$TGT" snapshots >/dev/null 2>&1; then
    if ! "$R" -r "$TGT" init >>"$LOG" 2>&1; then
      log "INIT FAILED $repo"
      rc_total=1
      continue
    fi
  fi
  # Залежавшийся эксклюзивный лок блокирует все чтения репозитория (дрилл и restore
  # в том числе). `unlock` без --remove-all трогает только устаревшие локи, живые
  # прогоны он не снимает.
  "$R" -r "$TGT" unlock >>"$LOG" 2>&1 || log "unlock $repo: не удалось (не критично)"
  if "$R" -r "$TGT" --from-repo "$SRC" --from-password-file "$srcpw" copy >>"$LOG" 2>&1; then
    n=$(count_snaps "$TGT")
    SNAPS[$repo]="${n:-0}"
    log "OK $repo (снапшотов в WORM: $n)"
  else
    log "COPY FAILED $repo"
    rc_total=1
  fi
done

if [ "$rc_total" = 0 ]; then
  write_hb 1 "ok"
else
  write_hb 0 "репликация завершилась с ошибкой (см. $LOG)"
fi
push_hb || rc_total=1

if [ "$rc_total" = 0 ]; then
  log "ИТОГ: ok, $(date -u +%FT%TZ)"
else
  log "ИТОГ: ПРОВАЛ"
fi
exit $rc_total
