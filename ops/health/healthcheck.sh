#!/usr/bin/env bash
# botkit uptime monitor: pings /health on all 9 bot ports, logs to a file,
# exits non-zero on any failure (so cron/job orchestrator can detect it).
set -u

HOST="${BOTKIT_HOST:-127.0.0.1}"
PORTS=(8081 8082 8083 8084 8085 8086 8087 8088 8089)
LOG="${BOTKIT_HEALTH_LOG:-/var/log/botkit-health.log}"
TIMEOUT=5

mkdir -p "$(dirname "$LOG")"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

ok=0
fail=0
failed_ports=()

for p in "${PORTS[@]}"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "http://${HOST}:${p}/health" 2>/dev/null)
  if [ "$code" = "200" ]; then
    ok=$((ok+1))
  else
    fail=$((fail+1))
    failed_ports+=("$p:$code")
    echo "$(ts) FAIL port=$p http=$code" >> "$LOG"
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "$(ts) OK ${ok}/${#PORTS[@]} ports healthy" >> "$LOG"
  exit 0
else
  echo "$(ts) FAIL ${ok}/${#PORTS[@]} ports healthy; down: ${failed_ports[*]}" >> "$LOG"
  exit 1
fi
