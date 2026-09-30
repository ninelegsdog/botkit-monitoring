#!/bin/bash
# Проверка свежести offsite-бэкапов (restic): метрика для Prometheus + алерт в Alertmanager
set -uo pipefail
PREFIX="[botkit-restic-check]"
PW_DIR=${PW_DIR:-/root/.botkit-backup}
BACKUP_HOST=${BACKUP_HOST:-31.76.11.198}
TEXTFILE=${TEXTFILE:-/var/lib/node-exporter-textfile/botkit_backup.prom}
ALERTMANAGER_URL=${ALERTMANAGER_URL:-http://127.0.0.1:9093}
MAXAGE_DATA=28800
MAXAGE_MONITOR=36000
RETRIES=3

declare -A REPO MAXAGE
REPO[data]="sftp:botkit-backup@$BACKUP_HOST:/repo-data"
REPO[monitor]="sftp:botkit-backup@$BACKUP_HOST:/repo-monitor"
MAXAGE[data]=$MAXAGE_DATA
MAXAGE[monitor]=$MAXAGE_MONITOR

tmp=$(mktemp) || { echo "$PREFIX не удалось создать временный файл" >&2; exit 1; }
{
  echo "# HELP botkit_backup_age_seconds Seconds since the last offsite restic snapshot"
  echo "# TYPE botkit_backup_age_seconds gauge"
  echo "# HELP botkit_backup_ok 1 if the offsite backup stream is fresh and reachable"
  echo "# TYPE botkit_backup_ok gauge"
} > "$tmp"

bad=()
for stream in data monitor; do
  pw="$PW_DIR/$stream.pw"
  last=""
  for _ in $(seq 1 $RETRIES); do
    last=$(RESTIC_PASSWORD_FILE="$pw" restic -r "${REPO[$stream]}" snapshots --json --latest 1 2>/dev/null \
      | python3 -c 'import json,sys,datetime
def _t(x):
    try:
        return datetime.datetime.fromisoformat(x["time"].replace("Z","+00:00"))
    except Exception:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
try:
    d=json.load(sys.stdin)
    print(max(d, key=_t)["time"] if d else "")
except Exception:
    print("")' 2>/dev/null)
    [ -n "$last" ] && break
    sleep 5
  done
  if [ -z "$last" ]; then
    echo "botkit_backup_age_seconds{stream=\"$stream\"} -1" >> "$tmp"
    echo "botkit_backup_ok{stream=\"$stream\"} 0" >> "$tmp"
    bad+=("$stream")
    echo "$PREFIX поток $stream: НЕДОСТУПЕН (список снапшотов не получен)" >&2
    continue
  fi
  age=$(python3 -c 'import sys,datetime
try:
    d=datetime.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00"))
    print(int((datetime.datetime.now(datetime.timezone.utc)-d).total_seconds()))
except Exception:
    print("")' "$last" 2>/dev/null)
  if [ -z "$age" ]; then
    echo "botkit_backup_age_seconds{stream=\"$stream\"} -1" >> "$tmp"
    echo "botkit_backup_ok{stream=\"$stream\"} 0" >> "$tmp"
    bad+=("$stream")
    echo "$PREFIX поток $stream: не удалось разобрать время снапшота '$last'" >&2
    continue
  fi
  ok=1
  if [ "$age" -gt "${MAXAGE[$stream]}" ]; then ok=0; bad+=("$stream"); fi
  echo "botkit_backup_age_seconds{stream=\"$stream\"} $age" >> "$tmp"
  echo "botkit_backup_ok{stream=\"$stream\"} $ok" >> "$tmp"
  echo "$PREFIX поток $stream: возраст ${age} с, порог ${MAXAGE[$stream]} с, ok=$ok" >&2
done

cat "$tmp" > "$TEXTFILE"
chmod 644 "$TEXTFILE"
rm -f "$tmp"
echo "$PREFIX метрики записаны в $TEXTFILE" >&2

if [ "${#bad[@]}" -gt 0 ]; then
  for s in "${bad[@]}"; do
    curl -sf -m 10 -XPOST -H 'Content-Type: application/json' "$ALERTMANAGER_URL/api/v2/alerts" \
      -d "[{\"labels\":{\"alertname\":\"BotkitBackupStale\",\"stream\":\"$s\",\"severity\":\"warning\"},\"annotations\":{\"summary\":\"Offsite-бэкап потока $s устарел или недоступен\"}}]" \
      >/dev/null 2>&1 || echo "$PREFIX не удалось отправить алерт в Alertmanager ($ALERTMANAGER_URL)" >&2
  done
  echo "$PREFIX ПРОВАЛ: потоки с проблемами: ${bad[*]}" >&2
  exit 1
fi
echo "$PREFIX все потоки в норме" >&2
exit 0
