#!/usr/bin/env bash
# botkit uptime monitor: pings /health on all 9 bot ports, then the monitoring
# stack endpoints, container states, failed systemd units and backup freshness.
# Logs to a file and exits non-zero on any failure, so the timer unit goes to
# `systemctl --failed` and the outage is visible without reading the log.
#
# The previous version only checked the nine bot ports - which is how a dead
# Prometheus and a failed backup unit stayed invisible for two days: every
# check that is absent from this script is a check the operator does not have.
set -u

HOST="${BOTKIT_HOST:-127.0.0.1}"
PORTS=(8081 8082 8083 8084 8085 8086 8087 8088 8089)
LOG="${BOTKIT_HEALTH_LOG:-/var/log/botkit-health.log}"
TIMEOUT=5
SELF_UNIT="botkit-healthcheck.service"
# Backup cadence is 6h; 7h leaves one missed run before the check goes red.
MAX_BACKUP_AGE="${MAX_BACKUP_AGE:-25200}"
MAX_EXPORT_AGE="${MAX_EXPORT_AGE:-25200}"

SERVICES=(
  "prometheus|http://127.0.0.1:9090/-/healthy"
  "grafana|http://127.0.0.1:3000/api/health"
  "alertmanager|http://127.0.0.1:9093/-/healthy"
  "loki|http://127.0.0.1:3100/ready"
)

mkdir -p "$(dirname "$LOG")"

ts() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
fail=0
note_fail() { echo "$(ts) FAIL $*" >>"$LOG"; fail=$((fail + 1)); }

now=$(date +%s)

http_code() { # $1=url
  curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$1" 2>/dev/null
}

# --- 1. bot ports -----------------------------------------------------------
ok=0
for p in "${PORTS[@]}"; do
  code=$(http_code "http://${HOST}:${p}/health")
  if [ "$code" = "200" ]; then
    ok=$((ok + 1))
  else
    note_fail "port=$p http=$code"
  fi
done
echo "$(ts) bots ${ok}/${#PORTS[@]}" >>"$LOG"

# --- 2. monitoring stack ----------------------------------------------------
for entry in "${SERVICES[@]}"; do
  name="${entry%%|*}"
  url="${entry#*|}"
  code=$(http_code "$url")
  case "$code" in
    200 | 204) ;;
    *) note_fail "service=$name http=$code url=$url" ;;
  esac
done

# --- 3. edge (nginx answers on 80/443; any HTTP status proves it is serving) -
for port in 80 443; do
  code=$(http_code "http://127.0.0.1:${port}/")
  if [ "$code" = "000" ]; then
    note_fail "nginx port=$port http=$code"
  fi
done

# --- 4. containers ----------------------------------------------------------
# docker is authoritative for "something died"; an exited container keeps its
# restart policy from bringing it back only if the policy itself was lost.
containers=$(docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null | grep -E 'Exited|Restarting|Created|Dead' || true)
if [ -n "$containers" ]; then
  while IFS= read -r line; do
    note_fail "container=${line}"
  done <<<"$containers"
fi

# --- 5. failed systemd units ------------------------------------------------
# The unit itself is excluded: when this script exits 1 systemd marks it failed
# and the next run would otherwise report its own expected failure forever.
units=$(systemctl --failed --plain --no-legend 2>/dev/null | awk '{print $1}' | grep -E '[.]service$' | grep -v "^${SELF_UNIT}$" || true)
if [ -n "$units" ]; then
  while IFS= read -r unit; do
    note_fail "unit=$unit"
  done <<<"$units"
fi

# --- 6. backup freshness ----------------------------------------------------
# Two independent freshness signals: the 6h cp copies the green checks already
# look at, and the consistent sqlite export that has been failing since 03.10.
newest_backup=$(find /home/deploy -maxdepth 3 -path '*/backups/bot.db.*' -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1)
if [ -z "$newest_backup" ]; then
  note_fail "backup=missing (нет ни одного bot.db.*)"
else
  age=$((now - ${newest_backup%.*}))
  [ "$age" -gt "$MAX_BACKUP_AGE" ] && note_fail "backup=stale age=${age}s limit=${MAX_BACKUP_AGE}s"
fi

newest_export=$(find /var/lib/botkit-restic-stage -path '*/export.*.db' -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1)
if [ -z "$newest_export" ]; then
  note_fail "export=missing (нет согласованных снимков в stage)"
else
  age=$((now - ${newest_export%.*}))
  [ "$age" -gt "$MAX_EXPORT_AGE" ] && note_fail "export=stale age=${age}s limit=${MAX_EXPORT_AGE}s"
fi

# --- verdict ----------------------------------------------------------------
if [ "$fail" -eq 0 ]; then
  echo "$(ts) OK bots=${ok}/${#PORTS[@]} services=${#SERVICES[@]} containers=clean units=clean backups=fresh" >>"$LOG"
  exit 0
fi
echo "$(ts) FAIL problems=${fail} bots=${ok}/${#PORTS[@]}" >>"$LOG"
exit 1
