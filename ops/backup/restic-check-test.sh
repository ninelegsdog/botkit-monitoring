#!/bin/bash
# Тест парсера ops/backup/restic-check.sh
# Инвариант 1: из многозаписного JSON (несколько групп путей) берётся САМАЯ СВЕЖАЯ запись.
# Тест умеет падать: со старым кодом d[0] свежая запись (идёт последней) игнорируется → ok=0 → FAIL.
#
# Инвариант 2: свежий restic-снапшот ничего не говорит о целостности базы внутри него.
# Отдельный поток consistent читает метрику запуска (RUNMETRIC, пишет restic-backup.sh)
# и обязан падать на трёх вещах: failures>0, протухший timestamp, отсутствующий файл.
# С 03.10 по 05.10.2026 согласованные экспорты падали у всех девяти ботов, а чекер
# печатал "все потоки в норме" — как раз потому, что файла метрики запуска он не читал.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
script="${1:-$here/restic-check.sh}"
fails=0

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/bin" "$tmpdir/pw"
echo pw > "$tmpdir/pw/data.pw"
echo pw > "$tmpdir/pw/monitor.pw"

# $1 = consistent_failures, $2 = возраст последнего прогона в секундах
write_run_metric() {
  printf 'botkit_backup_consistent_failures %s\nbotkit_backup_last_run_timestamp_seconds %s\n' \
    "$1" "$(( $(date +%s) - $2 ))" > "$tmpdir/run.prom"
}
write_run_metric 0 60

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

# run_case <выходной prom> [путь к метрике запуска; по умолчанию $tmpdir/run.prom]
run_case() {
  local out="$1" run="${2:-$tmpdir/run.prom}"
  PATH="$tmpdir/bin:$PATH" PW_DIR="$tmpdir/pw" TEXTFILE="$out" RUNMETRIC="$run" \
    ALERTMANAGER_URL="http://127.0.0.1:1" bash "$script" >/dev/null 2>"$tmpdir/err"
  echo $?
}

fake_restic
rc=$(run_case "$tmpdir/out.prom")
echo "== stderr =="; cat "$tmpdir/err" 2>/dev/null
echo "== метрики =="; cat "$tmpdir/out.prom" 2>/dev/null

if grep -q 'botkit_backup_ok{stream="data"} 1' "$tmpdir/out.prom" 2>/dev/null; then
  echo "PASS: data — выбрана свежая запись (ok=1)"
else
  echo "FAIL: data — свежая запись проигнорирована (ожидалось ok=1)"; fails=$((fails+1))
fi

if [ "$rc" -eq 0 ]; then
  echo "PASS: exit=0 при свежих данных"
else
  echo "FAIL: exit=$rc (ожидался 0)"; fails=$((fails+1))
fi

if grep -q '^botkit_backup_consistent_ok 1$' "$tmpdir/out.prom" 2>/dev/null; then
  echo "PASS: свежий прогон consistent → ok=1"
else
  echo "FAIL: метрика consistent_ok не выставлена в 1 при живом прогоне"; fails=$((fails+1))
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
if grep -q '^botkit_backup_consistent_ok 1$' "$tmpdir/out2.prom" 2>/dev/null; then
  echo "PASS: протухший снапшот не портит поток consistent (прогон свежий)"
else
  echo "FAIL: поток consistent задет протухшим снапшотом, хотя прогон свежий"; fails=$((fails+1))
fi

# негативный кейс 2: согласованные снимки не восстановились (failures>0)
write_run_metric 2 60
rc=$(run_case "$tmpdir/out3.prom")
if grep -q '^botkit_backup_consistent_ok 0$' "$tmpdir/out3.prom" 2>/dev/null; then
  echo "PASS: failures=2 → consistent_ok=0"
else
  echo "FAIL: failures>0 не переводит consistent_ok в 0"; fails=$((fails+1))
fi
if [ "$rc" -ne 0 ]; then
  echo "PASS: exit=$rc при failures>0 (ожидался не 0)"
else
  echo "FAIL: exit=0 при failures>0 — поток объявлен здоровым"; fails=$((fails+1))
fi

# негативный кейс 3: последний прогон протух (старше порога)
write_run_metric 0 $((28800 + 600))
rc=$(run_case "$tmpdir/out4.prom")
if grep -q '^botkit_backup_consistent_ok 0$' "$tmpdir/out4.prom" 2>/dev/null; then
  echo "PASS: протухший прогон → consistent_ok=0"
else
  echo "FAIL: возраст прогона не влияет на consistent_ok"; fails=$((fails+1))
fi
if [ "$rc" -ne 0 ]; then
  echo "PASS: exit=$rc при протухшем прогоне (ожидался не 0)"
else
  echo "FAIL: exit=0 при протухшем прогоне"; fails=$((fails+1))
fi

# негативный кейс 4: метрики запуска нет вовсе — тот самый сценарий 03–05.10,
# когда чекер зеленел, не читая поток, который падал.
rc=$(run_case "$tmpdir/out5.prom" "$tmpdir/absent.prom")
if grep -q '^botkit_backup_consistent_ok 0$' "$tmpdir/out5.prom" 2>/dev/null; then
  echo "PASS: нет метрики запуска → consistent_ok=0"
else
  echo "FAIL: отсутствие метрики запуска не роняет consistent_ok"; fails=$((fails+1))
fi
if [ "$rc" -ne 0 ]; then
  echo "PASS: exit=$rc без метрики запуска (ожидался не 0)"
else
  echo "FAIL: exit=0 без метрики запуска — тот самый ложный зелёный"; fails=$((fails+1))
fi

if [ "$fails" -eq 0 ]; then echo "ИТОГ: PASS"; exit 0; fi
echo "ИТОГ: FAIL ($fails)"; exit 1
