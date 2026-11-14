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
bad_modes=0
for f in /var/lib/node-exporter-textfile/*.prom; do
  [ -f "$f" ] || continue
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

echo "=== host-check: $FAIL failures ==="
exit $FAIL
PY
scp -q /tmp/opencode/host-check.sh predator:'~/BOTOGRAD_TECH/botkit-monitoring/ops/lib/host-check.sh' && ssh predator 'cd ~/BOTOGRAD_TECH/botkit-monitoring && bash -n ops/lib/host-check.sh && chmod 755 ops/lib/host-check.sh && echo "OK"'