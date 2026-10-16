#!/usr/bin/env bash
# External watchdog for the E2E tunnels.
#
# The 14.10.2026 incident this closes: the LAN blipped, ssh exited five times in
# 50s, and systemd's StartLimitBurst (5 per 300s) marked
# botkit-e2e-tunnel.service failed. `Restart=always` cannot help once the start
# limit is hit - only an outside actor can `reset-failed` and start it again, so
# the unit stayed dead ~4h while every E2E run silently failed to reach Telegram.
#
# This runs from a timer. For each tunnel it revives the unit when the unit is
# not active (failed/inactive) and restarts it when it is active but its local
# forward port has been missing for a while (a half-dead ssh that keeps the unit
# green while the forward is gone).
#
# The port check is meaningful because `ssh -D` binds the listener only after it
# connects: on a fresh start the unit is briefly active with no port, hence the
# grace period before a restart. `ServerAliveInterval` on the tunnel makes a
# half-dead link exit on its own, so this path is the backup, not the primary.
set -euo pipefail

# unit -> local port the forward binds and must be listening on
declare -A TUNNELS=(
  [botkit-e2e-tunnel.service]=11080
  [botkit-e2e-am-tunnel.service]=19093
)

# A unit active for less than this many seconds is still allowed to bind its
# port; do not restart it out from under a normal start.
GRACE_S=120

log() { logger -t e2e-tunnel-watchdog -- "$*"; }

uptime_s() { cut -d' ' -f1 /proc/uptime; }

# Seconds the unit has been active, or -1 when it is not active.
active_age_s() {
  local unit="$1" entered
  entered="$(systemctl show "$unit" -p ActiveEnterTimestampMonotonic --value 2>/dev/null)"
  [ -n "$entered" ] || { echo -1; return; }
  awk -v e="$entered" -v u="$(uptime_s)" 'BEGIN { printf "%d\n", u - entered / 1000000 }'
}

rc=0
for unit in "${!TUNNELS[@]}"; do
  port="${TUNNELS[$unit]}"

  # A unit this host does not carry (tunnel never set up) is not our business;
  # do not manufacture failures for something that was never installed.
  systemctl cat "$unit" >/dev/null 2>&1 || continue

  state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null || echo unknown)"
  case "$state" in
    active|activating|reloading)
      listening=1
      ss -H -ltn "sport = :$port" 2>/dev/null | grep -q . || listening=0
      if [ "$listening" -eq 1 ]; then
        continue
      fi
      age="$(active_age_s "$unit")"
      if [ "$age" -ge 0 ] && [ "$age" -lt "$GRACE_S" ]; then
        continue  # still starting; the port may not be bound yet
      fi
      log "restarting $unit: active ${age}s but :$port not listening"
      systemctl restart "$unit" || { log "restart FAILED for $unit"; rc=1; }
      ;;
    *)
      log "reviving $unit: state=$state (start limit or crash left it down)"
      systemctl reset-failed "$unit" 2>/dev/null || true
      systemctl start "$unit" || { log "start FAILED for $unit"; rc=1; }
      ;;
  esac
done

exit "$rc"
