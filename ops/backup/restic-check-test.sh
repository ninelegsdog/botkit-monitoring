#!/bin/bash
# Тест парсера ops/backup/restic-check.sh
# Инвариант: из многозаписного JSON (несколько групп путей) берётся САМАЯ СВЕЖАЯ запись.
# Тест умеет падать: со старым кодом d[0] свежая запись (идёт последней) игнорируется → ok=0 → FAIL.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
script="${1:-$here/restic-check.sh}"
fails=0

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/bin" "$tmpdir/pw"
echo pw > "$tmpdir/pw/data.pw"
echo pw > "$tmpdir/pw/monitor.pw"

fake_restic() {
  cat > "$tmpdir/bin/restic" <<'FAKE'
#!/bin/bash
now=$(date -u +%Y-%m-%dT%H:%M:%S.000000000Z)
printf '[{"time":"2020-01-01T00:00:00.000000000Z","hostname":"prod","paths":["/old"]},{"time":"%s","hostname":"prod","paths":["/new"]}]\n' "$now"
FAKE
  chmod +x "$tmpdir/bin/restic"
}

# фейковый curl (алерты не шлём наружу)
cat > "$tmpdir/bin/curl" <<'FAKE'
#!/bin/bash
exit 0
FAKE
chmod +x "$tmpdir/bin/curl"

run_case() {
  local out="$1"
  PATH="$tmpdir/bin:$PATH" PW_DIR="$tmpdir/pw" TEXTFILE="$out" \
    ALERTMANAGER_URL="http://127.0.0.1:1" bash "$script" >/dev/null 2>"$tmpdir/err"
  echo $?
}

fake_restic
run_case "$tmpdir/out.prom" >/dev/null
echo "== stderr =="; cat "$tmpdir/err" 2>/dev/null
echo "== метрики =="; cat "$tmpdir/out.prom" 2>/dev/null

if grep -q 'botkit_backup_ok{stream="data"} 1' "$tmpdir/out.prom" 2>/dev/null; then
  echo "PASS: data — выбрана свежая запись (ok=1)"
else
  echo "FAIL: data — свежая запись проигнорирована (ожидалось ok=1)"; fails=$((fails+1))
fi

rc=$(run_case "$tmpdir/out.prom")
if [ "$rc" -eq 0 ]; then
  echo "PASS: exit=0 при свежих данных"
else
  echo "FAIL: exit=$rc (ожидался 0)"; fails=$((fails+1))
fi

# негативный кейс: все записи старые → ok=0
cat > "$tmpdir/bin/restic" <<'FAKE'
#!/bin/bash
printf '[{"time":"2020-01-01T00:00:00.000000000Z","hostname":"prod","paths":["/a"]},{"time":"2020-01-02T00:00:00.000000000Z","hostname":"prod","paths":["/b"]}]\n'
FAKE
chmod +x "$tmpdir/bin/restic"
run_case "$tmpdir/out2.prom" >/dev/null
if grep -q 'botkit_backup_ok{stream="data"} 0' "$tmpdir/out2.prom" 2>/dev/null; then
  echo "PASS: устаревшие данные → ok=0"
else
  echo "FAIL: устаревшие данные не распознаны"; fails=$((fails+1))
fi

if [ "$fails" -eq 0 ]; then echo "ИТОГ: PASS"; exit 0; fi
echo "ИТОГ: FAIL ($fails)"; exit 1
