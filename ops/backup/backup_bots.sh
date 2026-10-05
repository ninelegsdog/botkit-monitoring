#!/usr/bin/env bash
# backup_bots.sh — sqlite (по ботам) + shared redis (один инстанс).
# systemd timer 6ч; статусы в /var/backups/botkit/status для check_backups.sh.
set -u
KEEP=14
TS=$(date +%F-%H%M)
BASE=/home/deploy
STATUS_DIR=/var/backups/botkit/status
ALERTED_DIR=/var/backups/botkit/alerted
mkdir -p "$STATUS_DIR" "$ALERTED_DIR"
FAIL=0
RC=botkit-shared-redis-redis-1

# Единый механизм согласованной копии, общий с offsite-потоком (restic-backup.sh):
# раньше здесь был обычный cp, дающий надорванную копию в момент записи.
_self=$(readlink -f "${BASH_SOURCE[0]}")
. "$(dirname "$_self")/../lib/snapshot.sh"

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

redis_rdb_ok() {
  local f="$1" sz magic
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  magic=$(head -c5 "$f" 2>/dev/null || echo "")
  [ "$magic" = "REDIS" ] && [ "${sz:-0}" -gt 0 ]
}

mark_ok()   { echo "$TS" > "$STATUS_DIR/$1.ok";  rm -f "$STATUS_DIR/$1.fail"; }
mark_fail() { echo "$TS" > "$STATUS_DIR/$1.fail"; rm -f "$STATUS_DIR/$1.ok"; FAIL=1; }

# --- shared redis ---
REDIS_DIR="$BASE/botkit-shared-redis"
REDIS_PASS=""
[ -f "$REDIS_DIR/.env" ] && REDIS_PASS=$(grep "^REDIS_PASSWORD=" "$REDIS_DIR/.env" | head -1 | cut -d= -f2-)
REDIS_OK=0
LS_A=$(docker exec "$RC" redis-cli -p 6379 ${REDIS_PASS:+-a "$REDIS_PASS"} lastsave 2>/dev/null || echo 0)
if docker exec "$RC" redis-cli -p 6379 ${REDIS_PASS:+-a "$REDIS_PASS"} bgsave >/dev/null 2>&1 && \
   docker exec "$RC" redis-cli -p 6379 ${REDIS_PASS:+-a "$REDIS_PASS"} info >/dev/null 2>&1; then
  for _ in $(seq 1 15); do
    LS_B=$(docker exec "$RC" redis-cli -p 6379 ${REDIS_PASS:+-a "$REDIS_PASS"} lastsave 2>/dev/null || echo 0)
    [ "${LS_B:-0}" -gt "${LS_A:-0}" ] 2>/dev/null && break
    sleep 1
  done
  mkdir -p "$REDIS_DIR/backups"
  if docker cp "$RC":/data/dump.rdb "$REDIS_DIR/backups/redis.rdb.$TS" >/dev/null 2>&1 \
     && redis_rdb_ok "$REDIS_DIR/backups/redis.rdb.$TS"; then
    REDIS_OK=1
    mark_ok botkit-shared-redis
    echo "shared-redis: redis=ok ($(stat -c%s "$REDIS_DIR/backups/redis.rdb.$TS" 2>/dev/null || echo 0) bytes)"
  else
    echo "shared-redis: dump invalid/missing"
    mark_fail botkit-shared-redis
  fi
else
  echo "shared-redis: SAVE failed"
  mark_fail botkit-shared-redis
fi
ls -1t "$REDIS_DIR/backups"/redis.rdb.* 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f

# --- per-bot sqlite ---
for d in "$BASE"/botkit-*/; do
  bot=$(basename "$d")
  # The name is not a safe test for "is this a bot". botkit-monitoring and
  # botkit-shared-redis were excluded here by name, and the fleet snapshot
  # directory /home/deploy/botkit-backups/ matched the same glob: from
  # 04.10.2026 06:31 it was reported as a failed bot and took the whole
  # service to exit 1 on every run, while all nine real backups succeeded.
  # Ask for evidence of a bot instead - its source tree or its database. A
  # real bot whose database has vanished still has src/, so the "no
  # data/bot.db" alarm below keeps working for the case it was written for.
  if [ ! -d "$d/src" ] && [ ! -f "$d/data/bot.db" ]; then
    echo "skip $bot (not a bot: no src/ and no data/bot.db)"
    continue
  fi
  mkdir -p "$d/backups"
  bdb_ok=0
  if [ -f "$d/data/bot.db" ]; then
    # Снимок тем же механизмом, что и offsite-поток: online-backup API во временный
    # файл /tmp контейнера (tmpfs) -> docker cp. Обычный cp здесь был до 05.10 и давал
    # надорванную копию, если база писалась в момент копирования; integrity_check
    # после cp такое не всегда ловит. Если контейнер не запущен - тот же API на хосте.
    if consistent_snapshot "$bot" /app/data/bot.db "$d/data/bot.db" "$d/backups/bot.db.$TS" \
       && sqlite_ok "$d/backups/bot.db.$TS"; then
      bdb_ok=1
    else
      echo "WARN $bot sqlite snapshot/integrity FAILED"
    fi
  else
    echo "WARN $bot no data/bot.db"
    mark_fail "$bot"
    continue
  fi
  if [ "$bdb_ok" -eq 1 ] && [ "$REDIS_OK" -eq 1 ]; then
    mark_ok "$bot"
    echo "$bot: sqlite=ok redis=ok(shared)"
  else
    mark_fail "$bot"
    echo "$bot: sqlite=$bdb_ok redis=$REDIS_OK -> FAIL"
  fi
  ls -1t "$d"/backups/bot.db.* 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
  # Легаси, о которых скрипт забыл. redis стал общим 02.09.2026 (botkit-shared-redis),
  # а staging переехал в /var/lib/botkit-restic-stage 10.09.2026. С тех пор в backups
  # каждого бота копятся redis.rdb.* и export.*.db, которые никто не пишет и никто
  # не удаляет: ротация выше трогает только bot.db.*. Плюс restic увозит их в
  # offsite-снапшоты, где они лежат рядом с каноническим общим дампом и мешают понять,
  # какой из них актуален. Удаляем здесь, чтобы не копились.
  find "$d/backups" -maxdepth 1 -type f \( -name "redis.rdb.*" -o -name "export.*.db" \) -delete 2>/dev/null || true
done

exit $FAIL
