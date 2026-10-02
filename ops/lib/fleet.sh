#!/usr/bin/env bash
# Single entry point for fleet.env in shell scripts (S1).
#
# Sourcing fleet.env directly from six places meant six copies of the path
# resolution and six opportunities to hardcode 127.0.0.1:9093 instead. The
# contract test ops/e2e/tests/test_fleet_contract.py fails if a script stops
# using this file or starts naming a port itself.
#
# Usage:
#   . "$(dirname "$0")/../lib/fleet.sh"     # or an absolute path
#   then read $FLEET, $PROMETHEUS_URL, $ALERTMANAGER_URL, ...
#
# BOTKIT_FLEET_ENV overrides the location, which is how the production copy under
# /root/botkit-webhook-check/ finds it.

# Resolve the library's own directory even when the caller was invoked via a
# symlinked script, so fleet.env is found from anywhere.
_fleet_lib_dir() {
  local src="${BASH_SOURCE[0]}"
  while [ -L "$src" ]; do
    local dir; dir=$(cd -P "$(dirname "$src")" && pwd)
    src=$(readlink "$src")
    [[ $src != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}

FLEET_LIB_DIR="${BOTKIT_FLEET_LIB_DIR:-$(_fleet_lib_dir)}"
# Same fallback chain the drift and smoke scripts carried before S1, so a
# production copy under /root/botkit-webhook-check/ keeps working.
FLEET_ENV="${BOTKIT_FLEET_ENV:-$FLEET_LIB_DIR/fleet.env}"
[ -f "$FLEET_ENV" ] || FLEET_ENV=/root/botkit-webhook-check/fleet.env

if [ ! -f "$FLEET_ENV" ]; then
  echo "FATAL: fleet.env not found at $FLEET_ENV" >&2
  return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1090  # path is dynamic by design
. "$FLEET_ENV"

# Fail loudly at load time rather than with an empty string later: a missing
# variable in a curl URL is a silent no-op, and "the alert was not sent" is the
# failure this whole project keeps having to undo.
for _required in \
  FLEET WEBHOOK_DOMAIN WEBHOOK_IP \
  BASE_URL PROMETHEUS_URL ALERTMANAGER_URL ALERTMANAGER_ALERTS_URL \
  GRAFANA_URL LOKI_URL TEMPO_URL MINIO_URL MINIO_CONSOLE_URL \
  ALERTMANAGER_ALERTS_ACTIVE_URL \
  OTLP_HTTP_URL OTLP_GRPC_URL \
  WG_OBSERVE_SUBNET WG_OBSERVE_PROD_IP WG_OBSERVE_MONITOR_IP WG_OBSERVE_PORT
do
  if [ -z "${!_required:-}" ]; then
    echo "FATAL: $_required is unset or empty in $FLEET_ENV" >&2
    return 1 2>/dev/null || exit 1
  fi
done
unset _required
