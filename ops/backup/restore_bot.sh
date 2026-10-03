#!/usr/bin/env bash
# restore_bot.sh <bot> — ВОССТАВЛЕНИЕ sqlite бота из последнего локального бэкапа.
#
# ВАЖНО: redis общий для всех девяти ботов (botkit-shared-redis-redis-1) с 02.09.2026.
# Раньше у каждого бота был свой сервис redis, и скрипт умел останавливать его вместе с
# ботом. Такой схемы больше нет, и старая версия скрипта была ловушкой: она искала дамп
# в /home/deploy/<bot>/backups/redis.rdb.*, куда ничего не пишется с 02.09, то есть
# молча брала дамп месячной давности; плюс делала `docker compose stop bot redis` и
# `compose cp redis:...`, чего в compose бота больше нет. Выглядело как рабочий
# инструмент, а при аварии восстановило бы устаревший дамп в общий redis.
#
# Поэтому redis здесь НЕ трогается по умолчанию: он общий, и его восстановление
# затрагивает все девять ботов. Для этого есть отдельный FORCE_REDIS=1.
#
# Требования к запуску:
#   FORCE=1        — без него только dry-run (ничего не меняется)
#   FORCE_REDIS=1  — дополнительно восстановить общий redis (затрагивает все 9 ботов)
#   FORCE_STALE=1  — разрешить дамп redis старше 24ч
#
# Использование:
#   /root/restore_bot.sh botkit-store                 # dry-run, ничего не меняет
#   FORCE=1 /root/restore_bot.sh botkit-store          # только sqlite
#   FORCE=1 FORCE_REDIS=1 /root/restore_bot.sh botkit-store
set -u

bot="${1:-}"
[ -z "$bot" ] && { echo "Usage: FORCE=1 [$0] <bot>"; exit 2; }

FORCE="${FORCE:-0}"
FORCE_REDIS="${FORCE_REDIS:-0}"
FORCE_STALE="${FORCE_STALE:-0}"

DEPLOY=/home/deploy
ENV_ROOT=/usr/local/etc/botkit
OVERRIDE_DIR=/var/lib/botkit-rollout/overrides
SHARED_REDIS_DIR="$DEPLOY/botkit-shared-redis"
SHARED_REDIS_CONTAINER=botkit-shared-redis-redis-1
STALE_MAX_H=$((24 * 3600))
STALE_MAX_LABEL="24ч"

# Имя бота идёт в пути и в docker compose - поэтому только буквы/цифры/дефис.
name="${bot#botkit-}"
case "$name" in
    ""|*[!a-z0-9-]*)
        echo "ABORT: недопустимое имя бота: $bot (ожидается botkit-<буквы|цифры|дефис>)"
        exit 2
        ;;
esac
d="$DEPLOY/$bot"
[ -d "$d" ] || { echo "ABORT: нет каталога бота: $d"; exit 1; }

compose_files=(-f "$d/deploy/compose.yml")
envf="$ENV_ROOT/$name.env"
[ -f "$envf" ] || { echo "ABORT: нет env-файла: $envf"; exit 1; }

# The image comes from IMAGE_TAG in the env file, the same single source deploy_rollout.sh
# writes. This used to append $OVERRIDE_DIR/$name.yml when it existed, which was two ways
# to pin an image and therefore two ways to disagree; the generated override outlived the
# rollout that wrote it, because the rollback path never removed it.
if [ -f "$OVERRIDE_DIR/$name.yml" ]; then
  echo "WARN: найден устаревший override $OVERRIDE_DIR/$name.yml - игнорируется."
  echo "      Образ берётся из IMAGE_TAG в $envf. Удалите override, если он больше не нужен."
fi

# The trailing brace here was a typo for "]" and it was invisible until an override was
# present: "${arr[@]}}" concatenates the last element with a literal brace, so compose
# received "support.yml}" as a filename and refused to start. Restoring a database is what
# this script is for, and the failure mode was "restore does not work" on exactly the bots
# that had a leftover file.
DC=(docker compose --env-file "$envf" "${compose_files[@]}")
SV="$("${DC[@]}" config --services 2>/dev/null | head -1)"
SV="${SV:-bot}"

# --- найти последний sqlite-бэкап бота ---
DB=$(ls -1t "$d"/backups/bot.db.* 2>/dev/null | head -1)
[ -n "$DB" ] || { echo "ABORT: нет sqlite-бэкапа в $d/backups/bot.db.*"; exit 1; }

# --- последний общий дамп redis ---
RD=$(ls -1t "$SHARED_REDIS_DIR"/backups/redis.rdb.* 2>/dev/null | head -1)

TS=$(date -u +%F-%H%M)

say() { printf '%s\n' "$*"; }

say "== restore_bot: $bot =="
say "  bot dir      : $d"
say "  env file     : $envf"
say "  compose      : ${compose_files[*]}"
say "  service      : $SV"
say "  sqlite бэкап : $DB ($(stat -c%s "$DB") байт, $(date -u -r "$DB" '+%F %H:%M') UTC)"

if [ -n "$RD" ]; then
  age=$(( $(date +%s) - $(date -u -r "$RD" +%s) ))
  say "  redis дамп   : $RD ($(stat -c%s "$RD") байт, $(date -u -r "$RD" '+%F %H:%M') UTC, возраст ${age}s)"
else
  say "  redis дамп   : НЕ НАЙДЕН в $SHARED_REDIS_DIR/backups/redis.rdb.*"
fi

# --- проверка целостности бэкапа ДО любых разрушающих действий ---
if ! python3 - "$DB" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    r = con.execute("PRAGMA integrity_check").fetchone()[0]
    n = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    con.close()
except Exception as e:
    print(f"  ОШИБКА чтения бэкапа: {e}")
    sys.exit(1)
if r != "ok":
    print(f"  ОШИБКА: integrity_check = {r}")
    sys.exit(1)
print(f"  integrity_check: ok, таблиц {n}")
sys.exit(0)
PY
then
  say "ABORT: sqlite-бэкап не проходит integrity_check — боевую базу не трогаю"
  exit 1
fi

# --- проверка, что выбран именно текущий бэкап, а не месячной давности ---
db_age=$(( $(date +%s) - $(date -u -r "$DB" +%s) ))
say "  возраст sqlite-бэкапа: ${db_age}s (KEEP=14, снимок каждые 6ч)"
if [ "$db_age" -gt "$STALE_MAX_H" ] && [ "$FORCE_STALE" != "1" ]; then
  say "ABORT: sqlite-бэкап старше $STALE_MAX_LABEL (${db_age}s). Проверь backup-bots.timer."
  say "       Если это осознанно — FORCE_STALE=1"
  exit 1
fi

if [ "$FORCE" != "1" ]; then
  say ""
  say "DRY-RUN. Ничего не изменено. Для реального восстановления:"
  say "  FORCE=1 $0 $bot"
  [ -n "$RD" ] && [ "$FORCE_REDIS" != "1" ] && say "  (redis общий для 9 ботов — для его восстановления нужно FORCE_REDIS=1)"
  exit 0
fi

say ""
say "[1/4] сохранить текущее состояние -> $d/backups/pre-restore.$TS.*"
mkdir -p "$d/backups"
cp "$d/data/bot.db" "$d/backups/pre-restore.$TS.bot.db" 2>/dev/null || say "  ВНИМАНИЕ: не удалось снять текущую базу"
if [ -n "$RD" ] && [ "$FORCE_REDIS" = "1" ]; then
  docker cp "$SHARED_REDIS_CONTAINER":/data/dump.rdb "$d/backups/pre-restore.$TS.redis.rdb" >/dev/null 2>&1 \
    || say "  ВНИМАНИЕ: не удалось снять текущий дамп redis"
fi

say "[2/4] остановить сервис $SV"
"${DC[@]}" stop "$SV" || { say "ABORT: не удалось остановить $SV"; exit 1; }

say "[3/4] заменить data/bot.db"
if ! cp "$DB" "$d/data/bot.db"; then
  say "ABORT: замена базы провалилась, возвращаю бота в работу"
  "${DC[@]}" start "$SV" >/dev/null 2>&1
  exit 1
fi

if [ "$FORCE_REDIS" = "1" ]; then
  if [ -z "$RD" ]; then
    say "ABORT: FORCE_REDIS=1, но общий дамп не найден"
    "${DC[@]}" start "$SV" >/dev/null 2>&1
    exit 1
  fi
  if [ "$age" -gt "$STALE_MAX_H" ] && [ "$FORCE_STALE" != "1" ]; then
    say "ABORT: дамп redis старше $STALE_MAX_LABEL (${age}s) — он затёр бы живое состояние всех ботов."
    say "       Если это осознанно — FORCE_STALE=1"
    "${DC[@]}" start "$SV" >/dev/null 2>&1
    exit 1
  fi
  say "     ВНИМАНИЕ: восстановление общего redis затрагивает ВСЕ 9 ботов"
  say "[4/4] остановить общий redis, подложить дамп, запустить"
  docker stop "$SHARED_REDIS_CONTAINER" >/dev/null 2>&1 || {
    say "ABORT: не удалось остановить $SHARED_REDIS_CONTAINER"
    "${DC[@]}" start "$SV" >/dev/null 2>&1; exit 1; }
  if ! docker cp "$RD" "$SHARED_REDIS_CONTAINER":/data/dump.rdb; then
    say "ABORT: не удалось подложить дамп в контейнер redis"
    docker start "$SHARED_REDIS_CONTAINER" >/dev/null 2>&1
    "${DC[@]}" start "$SV" >/dev/null 2>&1
    exit 1
  fi
  docker start "$SHARED_REDIS_CONTAINER" >/dev/null 2>&1
else
  say "[4/4] redis не трогаю (общий для 9 ботов; для этого нужен FORCE_REDIS=1)"
fi

say ""
say "запускаю сервис $SV"
"${DC[@]}" up -d --no-deps "$SV" >/dev/null 2>&1 || "${DC[@]}" start "$SV" >/dev/null 2>&1
say "готово. ПРОВЕРЬ: /root/restore_test.sh $bot  и  docker logs --tail 30 $bot"