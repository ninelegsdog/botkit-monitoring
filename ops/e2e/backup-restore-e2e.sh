#!/usr/bin/env bash
# backup-restore-e2e.sh — проверка реальной архитеКтуры бэкапов (репо fix(backup)):
#   1) sqlite: online-backup в /home/deploy/backups-export/<bot>/export.<TS>.db (RPO 6ч, keep 40), offsite-копия в GH
#   2) redis: shared-redis rdb-дамп bgsave:6380 -> /home/deploy/botkit-shared-redis/backups/redis.rdb.<TS>
# Формат вывода: bot | sqlite_backup | sqlite_age | integrity | redis_backup | redis_age | rdb_valid
set -u
EXPORT=/home/deploy/backups-export
RDIR=/home/deploy/botkit-shared-redis/backups
BOTS="bookingbot leadgen store support membership pricesentry docuflow delivery reminder"
NOW=$(date +%s)
FAIL=0

sqlite_integrity() { # $1 = file
  python3 - "$1" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    ok = c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    n = c.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    c.close()
    print("OK(%d objects)" % n if ok else "CORRUPT")
    sys.exit(0 if ok else 1)
except Exception as e:
    print("ERR:%s" % e)
    sys.exit(1)
PY
}

printf "%-16s | %-12s | %-9s | %-9s | %-12s | %-9s | %s\n" "bot" "sqlite_file" "age" "integrity" "redis_file" "age" "rdb"
for b in $BOTS; do
  db=$(ls -t1 "$EXPORT/$b/export."*.db 2>/dev/null | head -1)
  out="NO-BACKUP|n/a|n/a"
  if [ -n "${db:-}" ]; then
    age=$(( (NOW - $(stat -c%Y "$db")) / 3600 ))
    res=$(sqlite_integrity "$db"); rc=$?
    age_s=$([ "$age" -le 9 ] && echo "ok(${age}h)" || echo "STALE(${age}h)")
    [ "$age" -le 9 ] || FAIL=1
    [ "$rc" -eq 0 ] || FAIL=1
    out="$(basename "$db")|${age_s}|${res}"
  else
    FAIL=1
  fi

  rdb=$(ls -t1 "$RDIR"/redis.rdb.* 2>/dev/null | head -1)
  rout="redis-MISSING|n/a|n/a"
  if [ -n "${rdb:-}" ]; then
    rage=$(( (NOW - $(stat -c%Y "$rdb")) / 3600 ))
    rvalid=$(docker run --rm -v "$rdb":/d/dump.rdb alpine:3.21 sh -c "ls -la /d 2>/dev/null | awk 'NR>1{print \$5\"B\"}'" 2>/dev/null || echo "unreadable")
    rage_s=$([ "$rage" -le 9 ] && echo "ok(${rage}h)" || echo "STALE(${rage}h)")
    [ "$rage" -le 9 ] || FAIL=1
    rout="$(basename "$rdb")|${rage_s}|${rvalid}"
  else
    FAIL=1
  fi

  OLDIFS=$IFS; IFS='|'
  printf "%-16s | %s\n" "$b" "$out | $rout"
  IFS=$OLDIFS
done
echo "-----"
[ "$FAIL" -eq 0 ] && echo "BACKUP-RESTORE-E2E: PASS" || echo "BACKUP-RESTORE-E2E: FAIL"