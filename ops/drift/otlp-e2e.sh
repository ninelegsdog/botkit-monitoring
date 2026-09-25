#!/usr/bin/env bash
# otlp-e2e.sh — синтетический span через otel-collector (OTLP-HTTP 0.0.0.0:4318)
# до Tempo, с проверкой появления traceID в Tempo /api/traces/<hex>.
#
# Семантика (Fail loudly):
#   - HTTP не 200 на /v1/traces                -> alert BotkitTraceMissing (critical)
#   - traceID не найден в Tempo за RETRIES    -> alert BotkitTraceMissing (critical)
#   - попытки считаются; после успеха purge watch-файла -> алерт resolved.
# Лог: /var/log/botkit-otlp-e2e.log; троттлинг 30 мин.
# Запуск: systemd timer каждые 15 мин как root на ПРОДЕ.
set -uo pipefail

OTLP_URL="http://127.0.0.1:4318/v1/traces"
TEMPO_URL="http://127.0.0.1:3200"
SERVICE="verif-probe"
SPAN_NAME="verif-e2e"

LOG=/var/log/botkit-otlp-e2e.log
STATE_DIR=/var/backups/botkit-otlp-e2e
ALERTED_DIR="$STATE_DIR/alerted"
AM_URL="http://127.0.0.1:9093/api/v2/alerts"
THROTTLE=1800
RETRIES=5
SLEEP_S=3

mkdir -p "$ALERTED_DIR"
now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(now) $*" >> "$LOG"; }

send_alert() { # $1=sev $2=reason
  local sev="$1" reason="$2" last="$ALERTED_DIR/last" cur age
  cur=$(date +%s)
  if [ -f "$last" ]; then
    age=$(( cur - $(stat -c %Y "$last") ))
    [ "$age" -lt "$THROTTLE" ] && { log "ALERT throttled ($reason)"; return 0; }
  fi
  local payload
  payload="[{\"labels\":{\"alertname\":\"BotkitTraceMissing\",\"severity\":\"$sev\",\"service\":\"$SERVICE\",\"bot\":\"otlp-e2e\"},\"annotations\":{\"summary\":\"OTLP e2e span not in Tempo\",\"description\":\"${reason:0:200}\"}}]"
  curl -s -o /dev/null -X POST -d "$payload" -H "Content-Type: application/json" "$AM_URL" --max-time 5 \
    && { echo "$(now)" > "$last"; log "ALERT sent ($sev): $reason"; } \
    || log "ALERT send FAILED: $reason"
}

# --- generate 32-hex traceId + 16-hex spanId (nanos now)
now_ns=$(date +%s%N)
trace_id=$(printf '%032x' "$now_ns")
span_id=$(printf '%016x' $(( (now_ns >> 8) & 0xFFFFFFFFFFFF )))
start_ms=$(date +%s)000
dur_ms=$((RANDOM % 40 + 10))
start_ns=$(( now_ns - dur_ms*1000000 ))
end_ns=$now_ns

# --- OTLP-HTTP JSON payload (v1/traces), otlp-json
payload=$(cat <<EOF
{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"$SERVICE"}}]},"scopeSpans":[{"spans":[{"traceId":"$trace_id","spanId":"$span_id","name":"$SPAN_NAME","kind":1,"startTimeUnixNano":"$start_ns","endTimeUnixNano":"$end_ns"}]}]}]}
EOF
)

log "trace $trace_id span $span_id send -> $OTLP_URL"
http=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST "$OTLP_URL" \
  -H "Content-Type: application/json" -d "$payload")

if [ "$http" != "200" ]; then
  log "SEND http=$http"
  send_alert critical "OTLP POST $OTLP_URL http=$http (collector down?)"
  exit 1
fi

# --- wait & verify trace in Tempo
for i in $(seq 1 "$RETRIES"); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$TEMPO_URL/api/traces/$trace_id")
  if [ "$code" = "200" ]; then
    rm -f "$ALERTED_DIR/last"
    log "VERIFIED trace $trace_id in Tempo (attempt $i)"
    echo "OTLP-E2E: PASS (trace $trace_id)"
    exit 0
  fi
  sleep "$SLEEP_S"
done

log "TRACE $trace_id NOT found in Tempo after $((RETRIES*SLEEP_S))s"
send_alert critical "trace $trace_id absent in Tempo after $((RETRIES*SLEEP_S))s (http=$http)"
echo "OTLP-E2E: FAIL (trace $trace_id missing)"
exit 1