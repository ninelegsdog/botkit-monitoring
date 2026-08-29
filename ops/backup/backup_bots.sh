#!/usr/bin/env bash
#
# backup_bots.sh — резервное копирование sqlite + redis для всех botkit-ботов.
# Запускается systemd timer каждые 6ч. Проверяет целостность и пишет статус
# в /var/backups/botkit/status/<bot>.ok | <bot>.fail для check_backups.sh.
#
set -u
KEEP=14
TS=$(date +%F-%H%M)
BASE=/home/deploy
STATUS_DIR=/var/backups/botkit/status
ALERTED_DIR=/var/backups/botkit/alerted
mkdir -p "$STATUS_DIR" "$ALERTED_DIR"
FAIL=0

sqlite_ok() {
  python3 - "$1" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    r = c.execute("PRAGMA integrity_check").fetchall()
    c.close()
    sys.exit(0 if r == [('ok',)] else 1)
except Exception:
    sys.exit(1)
PY
}

for d in "$BASE"/botkit-*/; do
  bot=$(basename "$d")
  [ "$bot" = "botkit-monitoring" ] && continue
  envf="$d/.env"
  [ -f "$envf" ] || { echo "skip $bot (no .env)"; continue; }
  pass=$(grep "^REDIS_PASSWORD=" "$envf" | head -1 | cut -d= -f2-)
  mkdir -p "$d/backups"
  bdb_ok=0; brdb_ok=0; dbsz=0; rdbsz=0

  if [ -f "$d/data/bot.db" ]; then
    cp "$d/data/bot.db" "$d/backups/bot.db.$TS"
    dbsz=$(stat -c%s "$d/backups/bot.db.$TS" 2>/dev/null || echo 0)
    if sqlite_ok "$d/backups/bot.db.$TS"; then
      bdb_ok=1
    else
      echo "WARN $bot sqlite integrity FAILED"; FAIL=1
    fi
  else
    echo "WARN $bot no data/bot.db"; FAIL=1
  fi

  if [ -n "$pass" ]; then
    ( cd "$d" && docker compose exec -T redis redis-cli -a "$pass" save >/dev/null 2>&1 )
    ( cd "$d" && docker compose cp redis:/data/dump.rdb "$d/backups/redis.rdb.$TS" >/dev/null 2>&1 )
    if [ -f "$d/backups/redis.rdb.$TS" ]; then
      rdbsz=$(stat -c%s "$d/backups/redis.rdb.$TS" 2>/dev/null || echo 0)
      magic=$(head -c5 "$d/backups/redis.rdb.$TS" 2>/dev/null || echo "")
      if [ "$magic" = "REDIS" ] && [ "${rdbsz:-0}" -gt 0 ]; then
        brdb_ok=1
      else
        echo "WARN $bot redis dump invalid (magic=$magic size=$rdbsz)"; FAIL=1
      fi
    else
      echo "WARN $bot redis dump missing"; FAIL=1
    fi
  else
    echo "WARN $bot no REDIS_PASSWORD"; FAIL=1
  fi

  ls -1t "$d/backups"/bot.db.* 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
  ls -1t "$d/backups"/redis.rdb.* 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f

  if [ "$bdb_ok" -eq 1 ] && [ "$brdb_ok" -eq 1 ]; then
    echo "$TS" > "$STATUS_DIR/$bot.ok"
    rm -f "$STATUS_DIR/$bot.fail"
    echo "$bot: sqlite=ok redis=ok"
  else
    echo "$TS" > "$STATUS_DIR/$bot.fail"
    rm -f "$STATUS_DIR/$bot.ok"
    echo "$bot: sqlite=$bdb_ok redis=$brdb_ok -> FAIL"
  fi
done

exit $FAIL
