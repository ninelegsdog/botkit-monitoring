#!/bin/bash
# Проверка свежести offsite-бэкапов (restic): метрика для Prometheus + алерт в Alertmanager
set -uo pipefail
PREFIX="[botkit-restic-check]"
PW_DIR=${PW_DIR:-/root/.botkit-backup}
BACKUP_HOST=${BACKUP_HOST:-31.76.11.198}
TEXTFILE=${TEXTFILE:-/var/lib/node-exporter-textfile/botkit_backup.prom}
# The loader is resolved through symlinks: /usr/local/sbin/botkit-restic-check is a
# link into the repository on the live host, and sourcing ../lib/fleet.sh relative to
# the link's own directory would look for /usr/local/lib/fleet.sh, which is not
# installed - that exact miss is what made the deployed copy fail every 15 minutes
# until the copy was replaced by the link.
_self=$(readlink -f "${BASH_SOURCE[0]}")
. "$(dirname "$_self")/../lib/fleet.sh"
# S1: was a hardcoded default. fleet.sh is the single source; the env var
# still wins so the unit can override it for a one-off.
ALERTS_URL=${ALERTS_URL:-$ALERTMANAGER_ALERTS_URL}
MAXAGE_DATA=28800
MAXAGE_MONITOR=36000
RETRIES=3

declare -A REPO MAXAGE
REPO[data]="sftp:botkit-backup@$BACKUP_HOST:/repo-data"
REPO[cp]="sftp:botkit-backup@$BACKUP_HOST:/repo-cp"
REPO[monitor]="sftp:botkit-backup@$BACKUP_HOST:/repo-monitor"
MAXAGE[data]=$MAXAGE_DATA
MAXAGE[cp]=$MAXAGE_DATA
MAXAGE[monitor]=$MAXAGE_MONITOR

tmp=$(mktemp) || { echo "$PREFIX не удалось создать временный файл" >&2; exit 1; }
{
  echo "# HELP botkit_backup_age_seconds Seconds since the last offsite restic snapshot"
  echo "# TYPE botkit_backup_age_seconds gauge"
  echo "# HELP botkit_backup_ok 1 if the offsite backup stream is fresh and reachable"
  echo "# TYPE botkit_backup_ok gauge"
} > "$tmp"

bad=()
for stream in data cp monitor; do
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

# Согласованные снимки: свежесть restic-снапшота ничего не говорит о том, что внутри
# него целая база. Поток data пишет метрику запуска отдельным файлом, и с 03.10 по
# 05.10 он падал у всех девяти ботов, пока здесь печаталось "все потоки в норме" -
# снапшот-то свежий, просто внутри него надорванные копии. Отдельная метрика, а не
# дубль botkit_backup_consistent_failures: два файла textfile-коллектора с одним именем
# метрики дают scrape-ошибку.
RUNMETRIC=${RUNMETRIC:-/var/lib/node-exporter-textfile/botkit_backup_run.prom}
MAXAGE_CONSISTENT=${MAXAGE_CONSISTENT:-28800}
{
  echo "# HELP botkit_backup_consistent_ok 1 if the last consistent-snapshot run succeeded and is fresh"
  echo "# TYPE botkit_backup_consistent_ok gauge"
  echo "# HELP botkit_backup_consistent_age_seconds Seconds since the last consistent-snapshot run"
  echo "# TYPE botkit_backup_consistent_age_seconds gauge"
} >> "$tmp"

c_ok=1
c_age=-1
c_reason=""
if [ ! -r "$RUNMETRIC" ]; then
  c_ok=0
  c_reason="метрика запуска $RUNMETRIC не читается"
else
  cfails=$(awk '/^botkit_backup_consistent_failures /{print $2}' "$RUNMETRIC" | head -1)
  clast=$(awk '/^botkit_backup_last_run_timestamp_seconds /{print $2}' "$RUNMETRIC" | head -1)
  case "$clast" in
    ''|*[!0-9]*)
      c_ok=0; c_reason="нет корректного last_run_timestamp в $RUNMETRIC" ;;
    *)
      c_age=$(( $(date +%s) - clast ))
      if [ -n "$cfails" ] && [[ "$cfails" =~ ^[0-9]+$ ]] && [ "$cfails" -gt 0 ]; then
        c_ok=0; c_reason="$cfails согласованных снимков не удалось"
      elif [ "$c_age" -gt "$MAXAGE_CONSISTENT" ]; then
        c_ok=0; c_reason="последний прогон ${c_age}s назад (порог ${MAXAGE_CONSISTENT}s)"
      fi
      ;;
  esac
fi
echo "botkit_backup_consistent_ok $c_ok" >> "$tmp"
echo "botkit_backup_consistent_age_seconds $c_age" >> "$tmp"
echo "$PREFIX поток consistent: возраст ${c_age} с, порог ${MAXAGE_CONSISTENT} с, ok=$c_ok${c_reason:+ ($c_reason)}" >&2
if [ "$c_ok" != "1" ]; then
  bad+=("consistent")
fi

cat "$tmp" > "$TEXTFILE"
chmod 644 "$TEXTFILE"
rm -f "$tmp"
echo "$PREFIX метрики записаны в $TEXTFILE" >&2

if [ "${#bad[@]}" -gt 0 ]; then
  for s in "${bad[@]}"; do
    curl -sf -m 10 -XPOST -H 'Content-Type: application/json' "$ALERTS_URL" \
      -d "[{\"labels\":{\"alertname\":\"BotkitBackupStale\",\"stream\":\"$s\",\"severity\":\"warning\"},\"annotations\":{\"summary\":\"Offsite-бэкап потока $s устарел или недоступен\"}}]" \
      >/dev/null 2>&1 || echo "$PREFIX не удалось отправить алерт в Alertmanager ($ALERTS_URL)" >&2
  done
  echo "$PREFIX ПРОВАЛ: потоки с проблемами: ${bad[*]}" >&2
  exit 1
fi
echo "$PREFIX все потоки в норме" >&2
exit 0
