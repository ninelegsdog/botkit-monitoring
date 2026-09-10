#!/usr/bin/env bash
#
# check_backups.sh — проверяет свежесть и целостность бэкапов.
# Запускается systemd timer каждый час. Если у бота нет свежего .ok статуса
# (старше 7ч) или есть .fail — шлёт алерт в Alertmanager (localhost:9093) и
# выходит с кодом 1. Алерты троттлятся: повторно не чаще раза в 6ч на бота.
#
set -u
STATUS_DIR=/var/backups/botkit/status
ALERTED_DIR=/var/backups/botkit/alerted
AM_URL="http://localhost:9093/api/v2/alerts"
MAX_AGE=25200   # 7h (timer бэкапа = 6h)
THROTTLE=21600  # 6h между повторными алертами
BOTS=$(for d in /home/deploy/botkit-*/; do b=$(basename "$d"); [ "$b" = "botkit-monitoring" ] && continue; echo "$b"; done)
NOW=$(date +%s)
PROBLEMS=0

send_alert() {
  local bot="$1" reason="$2"
  local last="$ALERTED_DIR/$bot"
  if [ -f "$last" ]; then
    local age=$(( NOW - $(stat -c %Y "$last") ))
    [ "$age" -lt "$THROTTLE" ] && return 0
  fi
  local ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local payload="[{\"labels\":{\"alertname\":\"BackupProblem\",\"severity\":\"critical\",\"bot\":\"$bot\",\"service\":\"botkit-backup\"},\"annotations\":{\"summary\":\"Backup problem: $bot\",\"description\":\"$reason (checked $ts)\"},\"generatorURL\":\"file:///root/check_backups.sh\"}]"
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
echo "RESULT: all backups fresh"
exit 0
