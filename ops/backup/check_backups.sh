#!/usr/bin/env bash
#
# check_backups.sh — проверяет свежесть и целостность бэкапов.
# Запускается systemd timer каждый час. Если у бота нет свежего .ok статуса
# (старже 7ч) или есть .fail — шлёт алерт в Alertmanager (адрес из fleet.env) и
# выходит с кодом 1. Алерты троттлятся: повторно не чаще раза в 6ч на бота.
#
set -u

# S1 loader, resolved through symlinks: /root/check_backups.sh is a link into the
# repository, and dirname of the link itself would be /root, where ../lib/fleet.sh
# does not exist - the very first line of every manual run of this script failed on it.
_self=$(readlink -f "${BASH_SOURCE[0]}")
. "$(dirname "$_self")/../lib/fleet.sh"
STATUS_DIR=/var/backups/botkit/status
ALERTED_DIR=/var/backups/botkit/alerted
# S1: was http://localhost:9093/... - localhost can resolve to ::1 where
# nothing listens, which turns the curl into a silent no-op.
AM_URL="$ALERTMANAGER_ALERTS_URL"
MAX_AGE=25200   # 7h (timer бэкапа = 6h)
THROTTLE=21600  # 6h между повторными алертами
# Same rule as backup_bots.sh: a directory name is not evidence that something
# is a bot. botkit-monitoring, botkit-shared-redis and the fleet snapshot
# directory /home/deploy/botkit-backups/ all match botkit-*/ and were counted
# as bots. That is how a snapshot folder created on 04.10.2026 kept this
# checker reporting "1 bot(s) with backup problems" for hours on end while
# all nine real backups were fine - and the alert it tried to send for that
# went to the old localhost:9093 address in /root, where nothing listens.
# Require evidence of a bot instead: its source tree or its database.
BOTS=$(for d in /home/deploy/botkit-*/; do
  [ -d "$d/src" ] || [ -f "$d/data/bot.db" ] || continue
  basename "$d"
done)
NOW=$(date +%s)
PROBLEMS=0

send_alert() {
  local bot="$1" reason="$2"
  local last="$ALERTED_DIR/$bot"
  if [ -f "$last" ]; then
    local age=$(( NOW - $(stat -c %Y "$last") ))
    [ "$age" -lt "$THROTTLE" ] && return 0
  fi
  local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local payload="[{\"labels\":{\"alertname\":\"BackupProblem\",\"severity\":\"critical\",\"bot\":\"$bot\",\"service\":\"botkit-backup\"},\"annotations\":{\"summary\":\"Backup problem: $bot\",\"description\":\"$reason (checked $ts)\"}}]"
  curl -s -o /dev/null -XPOST "$AM_URL" -H 'Content-Type: application/json' -d "$payload" || true
  echo "$ts" > "$last"
}

for bot in $BOTS; do
  ok="$STATUS_DIR/$bot.ok"
  fail="$STATUS_DIR/$bot.fail"
  if [ -f "$fail" ]; then
    echo "PROBLEM $bot: last backup FAILED"
    send_alert "$bot" "last backup failed (status .fail present)"
    PROBLEMS=$((PROBLEMS+1))
    continue
  fi
  if [ ! -f "$ok" ]; then
    echo "PROBLEM $bot: no backup status file"
    send_alert "$bot" "no backup status (.ok missing) — backup never succeeded"
    PROBLEMS=$((PROBLEMS+1))
    continue
  fi
  age=$(( NOW - $(stat -c %Y "$ok") ))
  if [ "$age" -gt "$MAX_AGE" ]; then
    echo "PROBLEM $bot: backup stale (${age}s old)"
    send_alert "$bot" "backup older than 7h (${age}s)"
    PROBLEMS=$((PROBLEMS+1))
    continue
  fi
  echo "OK $bot (backup age ${age}s)"
done

if [ "$PROBLEMS" -gt 0 ]; then
  echo "RESULT: $PROBLEMS bot(s) with backup problems"
  exit 1
fi

# Согласованные снимки проверяются отдельно от .ok-файлов выше: те описывают cp-копии,
# которые не ломались никогда. С 03.10 по 05.10 именно согласованные экспорты падали у
# всех девяти ботов (каталог backups/ остался root:root 700, а писал в него uid 1001),
# и при этом здесь красовалось "all backups fresh": свежая копия той же самой базы,
# могущей быть надорванной, не доказывает, что её вообще можно восстановить. Метрику
# запуска пишет сам бэкапер restic-backup.sh, вторая свежесть держится на её timestamp.
RUNMETRIC=${RUNMETRIC:-/var/lib/node-exporter-textfile/botkit_backup_run.prom}
MAX_AGE_RUN=${MAX_AGE_RUN:-28800}  # 8h: поток data ходит каждые 6ч, две пропущенные смены = аларм
CONSISTENT_PROBLEM=
RUN_AGE=

if [ ! -r "$RUNMETRIC" ]; then
  CONSISTENT_PROBLEM="нет метрики запуска в $RUNMETRIC (поток data не запускался?)"
else
  cfails=$(awk '/^botkit_backup_consistent_failures /{print $2}' "$RUNMETRIC" | head -1)
  clast=$(awk '/^botkit_backup_last_run_timestamp_seconds /{print $2}' "$RUNMETRIC" | head -1)
  case "$clast" in
    ''|*[!0-9]*) CONSISTENT_PROBLEM="в $RUNMETRIC нет корректного last_run_timestamp" ;;
    *)
      RUN_AGE=$(( NOW - clast ))
      if [ -z "$cfails" ] || ! [[ "$cfails" =~ ^[0-9]+$ ]]; then
        CONSISTENT_PROBLEM="в $RUNMETRIC нет корректного consistent_failures"
      elif [ "$cfails" -gt 0 ]; then
        CONSISTENT_PROBLEM="$cfails согласованных снимков не удалось в последнем прогоне"
      elif [ "$RUN_AGE" -gt "$MAX_AGE_RUN" ]; then
        CONSISTENT_PROBLEM="поток data не запускался ${RUN_AGE}s (порог ${MAX_AGE_RUN}s)"
      fi
      ;;
  esac
fi

if [ -n "$CONSISTENT_PROBLEM" ]; then
  echo "PROBLEM consistent-stream: $CONSISTENT_PROBLEM"
  send_alert "consistent-stream" "$CONSISTENT_PROBLEM"
  PROBLEMS=$((PROBLEMS+1))
else
  echo "OK consistent-stream (последний прогон ${RUN_AGE}s назад, failures=0)"
fi

if [ "$PROBLEMS" -gt 0 ]; then
  echo "RESULT: $PROBLEMS bot(s) with backup problems"
  exit 1
fi
echo "RESULT: all backups fresh"
exit 0
