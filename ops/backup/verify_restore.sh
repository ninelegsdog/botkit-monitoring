#!/usr/bin/env bash
#
# verify_restore.sh <bot> — ДОКАЗАТЬ, что восстановление удалось, а не что оно выполнилось.
#
# Зачем этот скрипт
# ----------------
# `restore_bot.sh` проверяет целостность бэкапа **до** того, как что-то трогать, и
# печатает «готово». `restore_test.sh` проверяет, что бэкап вообще загружается. Оба
# отвечают на вопрос «годен ли бэкап».
#
# Ни один не отвечает на вопрос «а восстановилось ли». После реального восстановления
# нужно убедиться, что:
#   1. база на месте именно из восстановленной копии, а не откатилась на старую;
#   2. она целостна;
#   3. количество строк не меньше, чем было в бэкапе (restore не должен терять данные);
#   4. бот действительно отвечает, а не просто «контейнер Up».
#
# Без этого тренировка измеряет время выполнения скрипта, а не способность вернуть
# сервис. Именно поэтому она и называлась «живой», а не доказательной.
#
# Использование:
#   /root/verify_restore.sh botkit-store            # снимок ДО, затем проверка
#   EXPECT_DB=/path/to/before.db /root/verify_restore.sh botkit-store
#
# Код возврата: 0 — восстановление доказано; 1 — не доказано (список расхождений).
set -uo pipefail

bot="${1:-}"
[ -z "$bot" ] && { echo "Usage: $0 <bot>"; exit 2; }
name="${bot#botkit-}"
# Overridable so the script can be exercised against a throwaway copy of the layout
# instead of production data - and so a restore drill on a clean machine can point
# at wherever it unpacked things.
DEPLOY="${DEPLOY:-/home/deploy}"
ENV_ROOT="${ENV_ROOT:-/usr/local/etc/botkit}"
d="$DEPLOY/botkit-$name"
[ -d "$d" ] || { echo "FAIL: нет каталога $d"; exit 1; }

fail=0
skipped=""
note() { printf '%s\n' "$*"; }
# Reasons contain spaces, so they are kept newline-separated and printed one per line.
skip() { skipped="${skipped}${skipped:+$'\n'}$1"; }
bad()  { printf 'FAIL: %s\n' "$*" >&2; fail=1; }

note "== verify_restore: $bot =="

# --- снимок «до» ------------------------------------------------------------
# Обычно это база, которую restore_bot.sh сохранил как pre-restore.*. Если её нет —
# сравнивать не с чем, и это само по себе стоит отметить, но не повод падать.
pre=$(ls -1t "$d"/backups/pre-restore.*.bot.db 2>/dev/null | head -1 || true)
if [ -n "$pre" ]; then
  note "  pre-restore снимок: $pre"
else
  skip "pre-restore снимок (сверять не с чем)"
fi

# --- 1. целостность текущей базы ------------------------------------------
live="$d/data/bot.db"
[ -f "$live" ] || { bad "нет живой базы $live"; exit 1; }
note "  живая база: $live ($(stat -c%s "$live") байт, $(stat -c%y "$live" | cut -d. -f1))"

integrity=$(python3 - "$live" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    r = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    print(r)
except Exception as e:
    print(f"ERROR {e}")
PY
)
if [ "$integrity" = "ok" ]; then
  note "  integrity_check: ok"
else
  bad "integrity_check: $integrity"
fi

# --- 2. сверка с источником -------------------------------------------------
# Сравниваем построчно: сколько строк было в том, что восстанавливали, и сколько
# стало. Меньше — восстановление потеряло данные. Больше — база не та.
compare() {
  python3 - "$1" "$2" <<'PY'
import sqlite3, sys

def counts(path):
    c = sqlite3.connect(path)
    tabs = [t[0] for t in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    out = {}
    for t in tabs:
        try:
            out[t] = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        except Exception:
            pass
    c.close()
    return out

src, dst = counts(sys.argv[1]), counts(sys.argv[2])
src_path, dst_path = sys.argv[1], sys.argv[2]
problems = []
if set(src) != set(dst):
    problems.append(f"набор таблиц различается: {sorted(set(src) ^ set(dst))}")
for t in sorted(set(src) & set(dst)):
    if dst[t] < src[t]:
        problems.append(f"{t}: стало меньше строк ({src[t]} -> {dst[t]})")
    elif dst[t] > src[t]:
        problems.append(f"{t}: строк больше источника ({src[t]} -> {dst[t]})")
for p in problems:
    print("DIFF", p)
print("ROWS", sum(src.values()), sum(dst.values()))
PY
}

# Newest periodic backup, excluding the pre-restore snapshot restore_bot.sh writes:
# comparing the live database against its own pre-restore copy would always match and
# would prove nothing.
newest_periodic_backup() {
  local f
  for f in "$d"/backups/bot.db.*; do
    [ -f "$f" ] || continue
    case "$f" in *pre-restore*) continue ;; esac
    printf '%s\n' "$f"
  done | sort -t. -k4,4r | head -1
}

src_db="${EXPECT_DB:-}"
if [ -z "$src_db" ]; then
  src_db=$(newest_periodic_backup)
fi
if [ -n "$src_db" ] && [ -f "$src_db" ]; then
  note "  сверка с источником: $src_db"
  cmp_out=$(compare "$src_db" "$live")
  while IFS= read -r line; do
    case "$line" in
      DIFF*) bad "${line#DIFF }" ;;
      ROWS*) note "  строк: источник ${line#ROWS }" ;;
    esac
  done <<<"$cmp_out"
else
  skip "источник для сверки строк не найден"
fi

# --- 3. бот отвечает ---------------------------------------------------------
# The bot's HTTP port lives in the per-bot env file under /usr/local/etc/botkit, as
# METRICS_PORT - a bare PORT= lookup finds nothing and silently skips the check.
envf="$ENV_ROOT/$name.env"
port=""
[ -f "$envf" ] && port=$(grep -oE '^[A-Z_]*PORT=[0-9]+' "$envf" | grep -oE '[0-9]+$' | head -1)
if [ -z "$port" ] && [ -f "$d/.env" ]; then
  port=$(grep -oE '^[A-Z_]*PORT=[0-9]+' "$d/.env" | grep -oE '[0-9]+$' | head -1)
fi
if [ -n "$port" ]; then
  if [ "${SKIP_HEALTH:-0}" = "1" ]; then
    skip "/health пропущен по требованию (SKIP_HEALTH=1)"
  else
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$port/health" 2>/dev/null)
    if [ "$code" = "200" ]; then
      note "  /health на порту $port: 200"
    else
      bad "/health на порту $port вернул $code"
    fi
  fi
else
  skip "порт бота не определён, /health не проверен"
fi

# --- вердикт ----------------------------------------------------------------
# Skipped checks are not passed checks. Reporting "proven" after verifying neither the
# row counts nor the bot's health is the same failure this project keeps undoing: a green
# result where the check never ran. An unproven restore must read as unproven.
if [ -n "${skipped:-}" ]; then
  note ""
  note "НЕ ПРОВЕРЕНО (это не провал, но и не доказательство):"
  while IFS= read -r reason; do
    [ -n "$reason" ] && note "  - $reason"
  done <<<"$skipped"
fi
if [ "$fail" -ne 0 ] || [ -n "${skipped:-}" ]; then
  printf '\nВОССТАНОВЛЕНИЕ НЕ ДОКАЗАНО.\n' >&2
  exit 1
fi
printf '\nВОССТАНОВЛЕНИЕ ДОКАЗАНО: целостность ok, строки сверены, бот отвечает.\n'
exit 0