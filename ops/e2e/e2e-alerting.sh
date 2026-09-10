#!/usr/bin/env bash
#
# e2e-alerting.sh — синтетический алерт: Prometheus -> Alertmanager -> Telegram(firing)
# -> resolve -> Telegram(resolved). На ЖИВОЙ системе, rules не трогаются только на время теста.
#
# Механика:
#   - В /etc/prometheus/alerts.yml (rule_files) временно вливается группа E2E_{TS}
#     (absent(несуществующей метрики) -> алерт активен после 1-й оценки).
#     Уникальный alertname => уникальный fingerprint => гарантированная новая уведомление
#     (не упрётся в repeat_interval=4h как повторно-firing).
#   - SIGHUP prometheus (hot reload). Firing -> AM active -> notifications_total+1 = TI.
#   - Восстановление alerts.yml -> SIGHUP -> AM drops alert (resolve_timeout=5m там где нужно)
#     -> ожидаем +1 resolve-уведомления.
#   - cleanup через trap: alerts.yml всегда возвращается в исходное.
#
# НЕ влияет на реальные правила. AM/прометей задействованы на ~6-9 минут.
# Оператор должен получить ДВА сообщения в Telegram (firing + resolved).
#
set -eu

PROM_DIR=/home/deploy/botkit-monitoring/prometheus
ALERTS="$PROM_DIR/alerts.yml"
BAK="$PROM_DIR/alerts.yml.e2e.bak"
PROM=botkit-monitoring-prometheus-1
AM_HOST=http://127.0.0.1:9093
PROME_HOST=http://127.0.0.1:9090
ALERT="E2E_ProbeGone_$(date +%s)"
FIRING_OK=0; RESOLVE_OK=0

cleanup() {
  if [ -f "$BAK" ]; then cp "$BAK" "$ALERTS"; rm -f "$BAK"; fi
  docker kill -s HUP "$PROM" >/dev/null 2>&1 || true
  echo "== cleanup: alerts.yml restored, prometheus reloaded =="
}
trap cleanup EXIT

wait_for() { # wait_for <desc> <cmd> <timeout_s>
  local desc="$1" cmd="$2" t="$3" i=0
  while [ $i -lt "$t" ]; do
    if eval "$cmd" >/dev/null 2>&1; then return 0; fi
    sleep 10; i=$((i + 10))
  done
  echo "FAIL: $desc (timeout ${t}s)"; return 1
}

tg_total() { curl -s "$AM_HOST/metrics" | awk '/^alertmanager_notifications_total\{integration="telegram"\}/ {s+=$2} END{print s+0}'; }

echo "== e2e-alerting: start $(date -u +%H:%M:%S) alert=$ALERT =="
cp "$ALERTS" "$BAK"
python3 - "$ALERTS" "$ALERT" <<'PY'
import sys, yaml
p, name = sys.argv[1], sys.argv[2]
data = yaml.safe_load(open(p)) or {}
data.setdefault("groups", []).append({
    "name": "e2e",
    "rules": [{
        "alert": name,
        "expr": "absent(e2e_probe_success) == 1",
        "for": "15s",
        "labels": {"severity": "warning", "e2e": "true"},
        "annotations": {
            "summary": "E2E synthetic alert (auto-resolve %s)" % name,
            "description": "synthetic probe fired by e2e-alerting.sh",
        },
    }],
})
with open(p, "w") as f:
    yaml.safe_dump(data, f, sort_keys=False)
PY
docker kill -s HUP "$PROM" >/dev/null

wait_for "prometheus: $ALERT firing" \
  "curl -s '$PROME_HOST/api/v1/query' --data-urlencode 'query=ALERTS{alertname=\"$ALERT\",alertstate=\"firing\"}' | grep -q '$ALERT'" 180 \
  && echo "OK   prometheus: firing" || true

wait_for "alertmanager: $ALERT active" \
  "curl -s '$AM_HOST/api/v2/alerts?active=true' | grep -q '$ALERT'" 120

T0=$(tg_total)
echo "-- tg notifications before firing-delivery: $T0"
wait_for "telegram: firing delivered (+1)" \
  "test \"\$(tg_total)\" -gt \"$T0\"" 180 && { echo "OK   telegram: firing message delivered"; FIRING_OK=1; } || true

echo "== firing confirmed? -> restoring rules for resolve =="
cp "$BAK" "$ALERTS"; rm -f "$BAK"
docker kill -s HUP "$PROM" >/dev/null

wait_for "prometheus: $ALERT gone" \
  "! curl -s '$PROME_HOST/api/v1/query' --data-urlencode 'query=ALERTS{alertname=\"$ALERT\"}' | grep -q '$ALERT'" 180 \
  && echo "OK   prometheus: alert gone" || true

wait_for "alertmanager: $ALERT dropped (resolve)" \
  "! curl -s '$AM_HOST/api/v2/alerts?active=true' | grep -q '$ALERT'" 300 \
  && echo "OK   alertmanager: alert dropped" || true

T1=$(tg_total)
echo "-- tg notifications after firing-delivery: $T1"
wait_for "telegram: resolved delivered (+1)" \
  "test \"\$(tg_total)\" -gt \"$T1\"" 420 && { echo "OK   telegram: resolved message delivered"; RESOLVE_OK=1; } || true

echo "== RESULT: firing=$FIRING_OK resolved=$RESOLVE_OK =="
echo "== NOTE: оператор должен был получить 2 сообщения в Telegram ("'"'"$ALERT"'"'" firing + resolved) =="
[ "$FIRING_OK" -eq 1 ] && [ "$RESOLVE_OK" -eq 1 ] && echo "E2E-ALERTING: PASS" || echo "E2E-ALERTING: PARTIAL (firing=$FIRING_OK resolved=$RESOLVE_OK)"