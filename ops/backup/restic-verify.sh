#!/bin/bash
# Ежеквартальная проверка самих ДАННЫХ restic + автотест восстановления в песочницу.
#
# botkit-restic-check (каждые 15 мин) смотрит метаданные и возраст снимков: он
# поймает недоступный репозиторий и протухший прогон, но не битые данные внутри
# снимка - восстановимость такое не проверяет. Это пункт 1.6 плана (Фаза 1.4 п.4):
# читать данные и реально восстанавливать, иначе о битости узнаём в день инцидента.
#
#   check --read-data-subset=5 - выборочно читает и сверяет хеши 5% данных;
#   restore latest в песочницу  - только для repo-data (БД ботов, ~2 МБ), после
#                                 проверки PRAGMA integrity_check по всем .db,
#                                 песочница удаляется.
#
# Метрики botkit_backup_verify_* пишутся в свой файл: у юнита botkit-restic-check
# свои имена - два юнита с одними именами конфликтуют в textfile-коллекторе.
# Скрипт самодостаточен (не sourcing'ит lib/fleet.sh): зависимости, которых нет в
# /usr/local/lib, уже роняли юнит каждые 15 минут - см. инцидент с копией чекера.
set -uo pipefail
PREFIX="[botkit-restic-verify]"
PW_DIR=${PW_DIR:-/root/.botkit-backup}
BACKUP_HOST=${BACKUP_HOST:-31.76.11.198}
TEXTFILE=${TEXTFILE:-/var/lib/node-exporter-textfile/botkit_backup_verify.prom}
DRILL_DIR=${DRILL_DIR:-/var/lib/botkit-restore-drill}
# restic 0.18: значение только в виде '5%' (или 'n/t'), голое '5' даёт
# "check flag --read-data-subset has invalid value" и гасит проверку целиком.
SUBSET=${SUBSET:-5%}

_self=$(readlink -f "${BASH_SOURCE[0]}")
BASENAME=$(basename "$_self")
declare -A REPO
REPO[data]="sftp:botkit-backup@$BACKUP_HOST:/repo-data"
REPO[cp]="sftp:botkit-backup@$BACKUP_HOST:/repo-cp"
REPO[monitor]="sftp:botkit-backup@$BACKUP_HOST:/repo-monitor"

log() { echo "$PREFIX $*"; }

case "$DRILL_DIR" in
  ""|"/") echo "$PREFIX ОШИБКА: неприемлемый DRILL_DIR=$DRILL_DIR" >&2; exit 2 ;;
esac

write_metrics() {
  local tmp
  tmp=$(mktemp) || { echo "$PREFIX не удалось создать временный файл" >&2; return 1; }
  {
    echo "# HELP botkit_backup_verify_ok 1 if the quarterly deep restic check (read-data + restore drill) passed"
    echo "# TYPE botkit_backup_verify_ok gauge"
    for s in data cp monitor; do
      echo "botkit_backup_verify_ok{stream=\"$s\"} ${OK[$s]:-0}"
    done
    echo "# HELP botkit_backup_verify_last_timestamp_seconds Unix time of the last quarterly deep check"
    echo "# TYPE botkit_backup_verify_last_timestamp_seconds gauge"
    for s in data monitor; do
      echo "botkit_backup_verify_last_timestamp_seconds{stream=\"$s\"} ${TS[$s]:-0}"
    done
  } > "$tmp"
  mv -f "$tmp" "$TEXTFILE"
  # mktemp даёт 600, а node-exporter читает от nobody — без 644 метрики невидимы
  chmod 644 "$TEXTFILE"
}

declare -A OK TS
failed=0
now=$(date +%s)

for stream in data cp monitor; do
  pw="$PW_DIR/$stream.pw"
  if [ ! -f "$pw" ]; then
    log "нет пароля $pw - $stream пропущен"
    OK[$stream]=0; failed=$((failed+1)); continue
  fi
  log "поток $stream: check --read-data-subset=$SUBSET"
  if restic -p "$pw" --repo "${REPO[$stream]}" check --read-data-subset="$SUBSET" \
       >"/tmp/restic-verify-$BASENAME-$stream.log" 2>&1; then
    OK[$stream]=1
    log "поток $stream: данные читаются и сходятся"
  else
    OK[$stream]=0
    failed=$((failed+1))
    log "поток $stream: ПРОВЕРКА ДАННЫХ НЕ ПРОШЛА:"
    tail -5 "/tmp/restic-verify-$BASENAME-$stream.log" >&2
  fi
  TS[$stream]=$now
done

# Автотест восстановления: только БД-поток - он маленький, и именно он
# восстанавливается в первую очередь при реальном инциденте.
if [ "${OK[data]:-0}" -eq 1 ]; then
  ts=$(date -u +%Y%m%d-%H%M%S)
  target="$DRILL_DIR/$ts"
  mkdir -p "$DRILL_DIR" || { log "не создать $DRILL_DIR"; OK[data]=0; failed=$((failed+1)); }
  # прошлогодняя песочница не должна накапливаться
  find "$DRILL_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null
  mkdir -p "$target"
  if restic -p "$PW_DIR/data.pw" --repo "${REPO[data]}" restore latest --target "$target" \
       >"/tmp/restic-verify-$BASENAME-drill.log" 2>&1; then
    dbs=()
    while IFS= read -r f; do dbs+=("$f"); done < <(find "$target" -type f -name "*.db")
    if [ "${#dbs[@]}" -eq 0 ]; then
      log "восстановление прошло, но .db в снимке не найдено"
      OK[data]=0; failed=$((failed+1))
    else
      bad=0
      for f in "${dbs[@]}"; do
        if ! python3 - "$f" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
sys.exit(0 if c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else 1)
PY
        then
          bad=$((bad + 1))
          log "битая база в снимке: $f"
        fi
      done
      if [ "$bad" -eq 0 ]; then
        log "автотест восстановления: ${#dbs[@]} баз из снимка прошли integrity_check"
      else
        OK[data]=0; failed=$((failed + 1))
      fi
    fi
  else
    log "автотест восстановления НЕ ПРОШЁЛ"
    tail -5 "/tmp/restic-verify-$BASENAME-drill.log" >&2
    OK[data]=0; failed=$((failed + 1))
  fi
  rm -rf "$target"
fi

write_metrics
if [ "$failed" -eq 0 ]; then
  log "итог: PASS"
  exit 0
fi
log "итог: FAIL (ошибок: $failed)"
exit 1
