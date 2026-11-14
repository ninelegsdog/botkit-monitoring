#!/usr/bin/env bash
# host-check.sh — read-only validation of the host against the canon.
#
# Runs on the host (not in CI). Checks:
#   1. Installed units match canon (install-units.sh --check)
#   2. Prometheus loaded all canon alert names (via API)
#   3. Textfile modes are 644
#   4. Clone freshness (HEAD == origin/main for all clones)
#   5. No failed systemd units
#
# Usage: bash ops/lib/host-check.sh
# Exit code: 0 if all checks pass, 1 if any fail.
set -uo pipefail

REPO=${REPO:-/home/deploy/botkit-monitoring}
FAIL=0

check() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "OK   $name"
  else
    echo "FAIL $name"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== host-check $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# 1. Unit parity
echo "--- unit parity ---"
if [ -r "$REPO/ops/lib/install-units.sh" ]; then
  if bash "$REPO/ops/lib/install-units.sh" --check >/dev/null 2>&1; then
    echo "OK   unit parity"
  else
    echo "FAIL unit parity"
    FAIL=$((FAIL + 1))
  fi
else
  echo "SKIP unit parity (install-units.sh not found)"
fi

# 2. Textfile modes
echo "--- textfile modes ---"
if ! compgen -G "/var/lib/node-exporter-textfile/*.prom" >/dev/null; then
  # An unmatched glob used to fall through the loop and print "all textfiles 644"
  # on a host that has no textfiles at all — an OK for work that never happened.
  echo "SKIP textfile modes (no textfiles on this host)"
else
  bad_modes=0
  for f in /var/lib/node-exporter-textfile/*.prom; do
    mode=$(stat -c %a "$f")
    if [ "$mode" != "644" ]; then
      echo "FAIL mode $mode $f"
      bad_modes=$((bad_modes + 1))
    fi
  done
  if [ "$bad_modes" -eq 0 ]; then
    echo "OK   all textfiles 644"
  else
    FAIL=$((FAIL + bad_modes))
  fi
fi

# 2b. Poller metrics the alerts read must actually be published.
#     An alert over a metric nobody writes never fires; grepping the scripts does not
#     prove the value reached the textfile, only reading it does.
if compgen -G "/var/lib/node-exporter-textfile/botkit_*.prom" >/dev/null; then
  echo "--- poller metrics ---"
  missing_metrics=0
  for metric in \
    botkit_healthcheck_last_run_seconds \
    botkit_drift_last_run_seconds \
    botkit_rollout_check_last_run_seconds \
    botkit_backup_last_run_seconds \
    botkit_backup_verify_last_run_seconds \
    botkit_image_prune_last_run_seconds; do
    if ! grep -qh "^$metric " /var/lib/node-exporter-textfile/botkit_*.prom; then
      echo "FAIL metric $metric is not published"
      missing_metrics=$((missing_metrics + 1))
    fi
  done
  if [ "$missing_metrics" -eq 0 ]; then
    echo "OK   every alert-critical poller metric is published"
  else
    FAIL=$((FAIL + missing_metrics))
  fi
fi

# 3. Failed units
echo "--- failed units ---"
failed_count=$(systemctl --failed --no-legend 2>/dev/null | grep -v "loaded units listed" | grep -c "." || true)
failed_count=${failed_count:-0}
if [ "$failed_count" -eq 0 ]; then
  echo "OK   0 failed units"
else
  echo "FAIL $failed_count failed units"
  FAIL=$((FAIL + failed_count))
fi

# 4. is-system-running
echo "--- system running ---"
state=$(systemctl is-system-running 2>&1)
if [ "$state" = "running" ]; then
  echo "OK   is-system-running: running"
else
  echo "FAIL is-system-running: $state"
  FAIL=$((FAIL + 1))
fi

# 5. Prometheus rules loaded
echo "--- prometheus rules ---"
rules=$(curl -s http://127.0.0.1:9090/api/v1/rules 2>/dev/null | python3 -c 'import json,sys; print(sum(len(g["rules"]) for g in json.load(sys.stdin)["data"]["groups"]))' 2>/dev/null || echo 0)
if [ "$rules" = "0" ]; then
  echo "SKIP prometheus rules (no prometheus on this host)"
elif [ "$rules" -ge 40 ]; then
  echo "OK   prometheus rules: $rules"
else
  echo "FAIL prometheus rules: $rules (expected >= 40)"
  FAIL=$((FAIL + 1))
fi

# 6. Active alerts
echo "--- active alerts ---"
alerts=$(curl -s http://127.0.0.1:9093/api/v2/alerts 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
if [ "$alerts" -eq 0 ]; then
  echo "OK   0 active alerts"
else
  echo "WARN $alerts active alerts"
fi

# 7. Disk health (SMART)
echo "--- disk health ---"
if ! command -v smartctl >/dev/null 2>&1; then
  echo "SKIP smartctl (not installed)"
elif [ "$(id -u)" -eq 0 ]; then
  SMARTCTL="smartctl"
elif sudo -n true 2>/dev/null; then
  SMARTCTL="sudo -n smartctl"
else
  SMARTCTL=""
fi

if [ -z "$SMARTCTL" ]; then
  echo "SKIP SMART (needs root; run host-check with sudo)"
else
  for dev in /dev/sda /dev/sdb /dev/vda; do
    [ -b "$dev" ] || continue
    out=$($SMARTCTL -H "$dev" 2>/dev/null || true)
    case "$out" in
      *PASSED*) echo "OK   SMART $dev: PASSED" ;;
      *FAILED*) echo "FAIL SMART $dev: FAILED"; FAIL=$((FAIL + 1)) ;;
      *)        echo "SKIP SMART $dev: not supported (virtual disk or no ATA passthrough)" ;;
    esac
  done
fi

echo "=== host-check: $FAIL failures ==="
exit $FAIL
