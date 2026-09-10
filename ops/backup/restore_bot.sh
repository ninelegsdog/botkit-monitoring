#!/usr/bin/env bash
#
# restore_bot.sh <bot> — ВОССТАНОВЛЕНИЕ бота из последнего бэкапа.
# ОСТАНАВЛИВАЕТ бота и redis, заменяет data/bot.db и redis dump.rdb,
# затем запускает обратно. ТРЕБУЕТ переменную FORCE=1 (защита от случайного
# запуска). Рекомендуется сначала сделать свежий бэкап и прогнать restore_test.sh.
#
set -u
bot="${1:-}"
[ -z "$bot" ] && { echo "Usage: FORCE=1 $0 <bot>"; exit 2; }
[ "${FORCE:-0}" = "1" ] || { echo "ABORT: set FORCE=1 to actually restore. Dry-run only."; exit 3; }
d="/home/deploy/$bot"
[ -d "$d" ] || { echo "no such bot dir: $d"; exit 1; }
envf="$d/.env"
pass=$(grep "^REDIS_PASSWORD=" "$envf" | head -1 | cut -d= -f2-)
DB=$(ls -1t "$d"/backups/bot.db.* 2>/dev/null | head -1)
RD=$(ls -1t "$d"/backups/redis.rdb.* 2>/dev/null | head -1)
[ -n "$DB" ] || { echo "NO sqlite backup"; exit 1; }
[ -n "$RD" ] || { echo "NO redis backup"; exit 1; }
TS=$(date +%F-%H%M)

echo "[1/5] stop bot + redis for $bot"
( cd "$d" && docker compose stop bot redis )

echo "[2/5] backup current state -> $d/backups/pre-restore.$TS"
cp "$d/data/bot.db" "$d/backups/pre-restore.$TS.bot.db" 2>/dev/null || true
( cd "$d" && docker compose exec -T redis redis-cli -a "$pass" save >/dev/null 2>&1 )
( cd "$d" && docker compose cp redis:/data/dump.rdb "$d/backups/pre-restore.$TS.redis.rdb" >/dev/null 2>&1 ) || true

echo "[3/5] restore sqlite -> $d/data/bot.db"
cp "$DB" "$d/data/bot.db"

echo "[4/5] restore redis dump"
( cd "$d" && docker compose cp "$RD" redis:/data/dump.rdb )

echo "[5/5] start bot + redis"
( cd "$d" && docker compose start redis bot )
echo "RESTORE DONE for $bot from $DB / $RD"
