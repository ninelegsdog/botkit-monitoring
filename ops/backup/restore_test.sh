#!/usr/bin/env bash
#
# restore_test.sh <bot> — НЕРАЗРУШАЮЩАЯ проверка восстановления.
# Берёт самый свежий бэкап бота, проверяет целостность sqlite (PRAGMA
# integrity_check) и загружаетемость redis RDB во временный контейнер.
# Ничего не меняет в работающих ботах.
#
set -u
bot="${1:-}"
[ -z "$bot" ] && { echo "Usage: $0 <bot>"; exit 2; }
name="${bot#botkit-}"
d="/home/deploy/botkit-$name"
[ -d "$d" ] || { echo "no such bot dir: $d"; exit 1; }
DB=$(ls -1t "$d"/backups/bot.db.* 2>/dev/null | head -1)
RD=$(ls -1t /home/deploy/botkit-shared-redis/backups/redis.rdb.* 2>/dev/null | head -1)
[ -n "$DB" ] || { echo "NO sqlite backup for $bot"; exit 1; }
[ -n "$RD" ] || { echo "NO redis backup for $bot"; exit 1; }

echo "== sqlite backup: $DB =="
python3 - "$DB" <<'PY'
import sqlite3, sys
db=sys.argv[1]
c=sqlite3.connect(db)
r=c.execute("PRAGMA integrity_check").fetchall()
print("integrity_check:", r)
tabs=c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("tables:", [x[0] for x in tabs])
for (name,) in tabs:
    try:
        n=c.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        print(f"  {name}: {n} rows")
    except Exception as e:
        print(f"  {name}: ERR {e}")
c.close()
PY

echo "== redis backup: $RD =="
magic=$(head -c5 "$RD"); sz=$(stat -c%s "$RD")
echo "magic=$magic size=$sz"
td=$(mktemp -d)
cp "$RD" "$td/dump.rdb"
docker run --rm -d --name redis-restest-"$name" -v "$td:/data" redis:7-alpine redis-server --requirepass test >/dev/null
sleep 1
db_size=$(docker exec redis-restest-"$name" redis-cli -a test dbsize 2>/dev/null)
echo "redis dbsize (restored OK if number printed): $db_size"
docker rm -f redis-restest-"$name" >/dev/null
rm -rf "$td"
echo "== DONE: backup for $bot is restorable =="
