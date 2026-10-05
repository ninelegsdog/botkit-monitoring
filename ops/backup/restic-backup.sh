#!/bin/bash
# Offsite-бэкап ботов и мониторинга: restic -> репозитории на мониторе (SFTP, chroot)
set -euo pipefail

PW_DIR=/root/.botkit-backup
KEY=/root/.ssh/id_ed25519_botkit_backup
BACKUP_HOST=31.76.11.198
LOCK=/run/lock/botkit-restic-backup.lock
PREFIX="[botkit-restic-backup]"
RETRIES=3
STAGE=/var/lib/botkit-restic-stage
STAGE_KEEP=14
BOTS="membership bookingbot reminder leadgen store support delivery docuflow pricesentry"
RUNMETRIC=/var/lib/node-exporter-textfile/botkit_backup_run.prom

[ $# -ge 1 ] || { echo "$PREFIX用法: $0 {data|monitor|all}" >&2; exit 2; }
exec 9>"$LOCK"
flock -n 9 || { echo "$PREFIX уже выполняется, повтор пропущен"; exit 0; }

log() { echo "$PREFIX $*"; }
die() { echo "$PREFIX ОШИБКА: $*" >&2; exit 1; }

# Общий механизм согласованной копии (см. ops/lib/snapshot.sh). Резолв через
# readlink -f: на живом хосте этот скрипт - симлинк в клон канона, и sourcing
# относительно самого симлинка искал бы /usr/local/lib/snapshot.sh.
_self=$(readlink -f "${BASH_SOURCE[0]}")
. "$(dirname "$_self")/../lib/snapshot.sh"

# Согласованный снимок SQLite через общий ops/lib/snapshot.sh: online-backup API
# внутри контейнера во временный файл его /tmp (tmpfs) -> docker cp на хост, либо
# хостовой python3, если контейнер не запущен. Обычный cp (как был в backup_bots.sh)
# даёт надорванную копию, если база пишется; запись в /app/backups хоста (как было
# здесь до 05.10) зависела от владельца каталога и на два дня тихо сломала все
# девять снимков. Владелец хостового каталога на результат больше не влияет.
stage_consistent() {
  local b src dest failed=0 prune ts
  ts=$(date -u +%Y-%m-%d-%H%M)
  install -d -m 700 "$STAGE"
  for b in $BOTS; do
    src="botkit-$b"
    if ! docker inspect "$src" >/dev/null 2>&1; then
      log "поток data: контейнер $src не найден"; failed=$((failed+1)); continue
    fi
    install -d -m 700 "$STAGE/$b"
    dest="$STAGE/$b/export.$ts.db"
    if ! consistent_snapshot "$src" "/app/data/bot.db" "/home/deploy/botkit-$b/data/bot.db" "$dest"; then
      log "поток data: $b — согласованный снимок не удался"; failed=$((failed+1)); continue
    fi
    prune=$(ls -t "$STAGE/$b/export."* 2>/dev/null | tail -n +$((STAGE_KEEP+1)))
    [ -n "$prune" ] && rm -f $prune
  done
  log "поток data: согласованных снимков готово, ошибок: $failed"
  return $failed
}

write_run_metric() {
  local failed="$1"
  {
    echo "# HELP botkit_backup_consistent_failures Bots whose consistent SQLite snapshot failed in the last run"
    echo "# TYPE botkit_backup_consistent_failures gauge"
    echo "botkit_backup_consistent_failures $failed"
    echo "# HELP botkit_backup_last_run_timestamp_seconds Unix time of the last backup run"
    echo "# TYPE botkit_backup_last_run_timestamp_seconds gauge"
    echo "botkit_backup_last_run_timestamp_seconds $(date +%s)"
  } > "$RUNMETRIC"
  chmod 644 "$RUNMETRIC"
}

collect_paths() {
  local stream="$1" d f s
  PATHS=()
  if [ "$stream" = data ]; then
    for d in /home/deploy/botkit-*/backups; do [ -d "$d" ] && PATHS+=("$d"); done
    for f in /home/deploy/botkit-monitoring/.env /home/deploy/reverse-proxy/.env /home/deploy/botkit-shared-redis/.env; do
      [ -f "$f" ] && PATHS+=("$f")
    done
    [ -d /usr/local/etc/botkit ] && PATHS+=(/usr/local/etc/botkit)
    [ -d "$STAGE" ] && PATHS+=("$STAGE")
  else
    local vols
    vols=$(for c in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep '^botkit-monitoring-'); do
      docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Source}}{{"\n"}}{{end}}{{end}}' "$c" 2>/dev/null
    done | sort -u)
    while IFS= read -r s; do [ -n "$s" ] && [ -d "$s" ] && PATHS+=("$s"); done <<< "$vols"
    [ -d /home/deploy/botkit-monitoring ] && PATHS+=(/home/deploy/botkit-monitoring)
  fi
}

run_stream() {
  local stream="$1" pw repo attempt rc=1
  case "$stream" in
    data)    pw="$PW_DIR/data.pw";    repo="sftp:botkit-backup@$BACKUP_HOST:/repo-data" ;;
    monitor) pw="$PW_DIR/monitor.pw"; repo="sftp:botkit-backup@$BACKUP_HOST:/repo-monitor" ;;
    *) die "неизвестный поток: $stream" ;;
  esac
  [ -r "$pw" ] || die "нет файла пароля $pw (создаётся при bootstrap)"
  [ -r "$KEY" ] || die "нет ключа $KEY"
  command -v docker >/dev/null || [ "$stream" = data ] || die "docker недоступен"

  local consistent_failures=0
  if [ "$stream" = data ]; then
    stage_consistent || consistent_failures=$?
  fi
  collect_paths "$stream"
  [ "${#PATHS[@]}" -gt 0 ] || die "поток $stream: источники не найдены"

  log "поток $stream: источников ${#PATHS[@]}, репозиторий $repo"
  for attempt in $(seq 1 $RETRIES); do
    if RESTIC_PASSWORD_FILE="$pw" restic -r "$repo" backup \
         --host prod --tag "$stream" --exclude-caches --exclude '*.tmp' "${PATHS[@]}"; then
      rc=0; break
    fi
    log "поток $stream: попытка $attempt/$RETRIES не удалась, пауза 30 с"
    sleep 30
  done
  if [ "$rc" -ne 0 ]; then
    write_run_metric "$consistent_failures"
    die "поток $stream: бэкап не удался после $RETRIES попыток"
  fi

  log "поток $stream: retention"
  if [ "$stream" = data ]; then
    # --retry-lock: если предыдущий запуск убили и остался stale-lock, forget падал
    # с кодом 11, retention не отрабатывал, и снапшоты копились без границы. Молча
    # ждать безопаснее, чем завершаться: реальный конкурент всё равно держит lock.
    RESTIC_PASSWORD_FILE="$pw" restic -r "$repo" forget --retry-lock 10m --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune
  else
    RESTIC_PASSWORD_FILE="$pw" restic -r "$repo" forget --retry-lock 10m --keep-daily 3 --keep-weekly 2 --prune
  fi
  if [ "$stream" = data ]; then
    write_run_metric "$consistent_failures"
    [ "$consistent_failures" -eq 0 ] || die "поток data: $consistent_failures согласованных снимков не удалось"
  fi
  log "поток $stream: готово"
}

case "$1" in
  all) run_stream data; run_stream monitor ;;
  data|monitor) run_stream "$1" ;;
  *) die "неизвестный аргумент: $1" ;;
esac
log "итог: успешно"
