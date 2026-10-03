#!/usr/bin/env bash
# deploy_rollout.sh — health-gated single-bot rollout with automatic rollback
# Usage: deploy_rollout.sh <bot> [image:tag] [--dry-run] [--failpoint=health]
#   <bot>            bookingbot|delivery|docuflow|leadgen|membership|pricesentry|reminder|store|support
#   image:tag        default "main"  (e.g. "v0.8.0-abc123" or digest)
#   --dry-run        print plan, touch nothing
#   --failpoint=health  force a simulated health failure to drill the rollback path (no alert)
# Runs on the prod VPS as root. Log: /var/log/botkit-rollout.log
#
# Imported to version control 2026-10-03. Until then this file lived only at
# /opt/botkit-rollout/deploy_rollout.sh, outside git, while the drift check in the same
# repository reported on it. The health gate, the rollback and the alert calls are unchanged;
# the only edit is where the Alertmanager URL comes from.
set -uo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/../lib/fleet.sh"

# Factual bot → port map (docker inspect METRICS_PORT, verified 2026-09-18)
declare -A PORTS=(
  [bookingbot]=8081 [leadgen]=8082 [store]=8083 [support]=8084
  [membership]=8085 [pricesentry]=8086 [docuflow]=8087
  [delivery]=8088 [reminder]=8089
)

REGISTRY="ghcr.io/ninelegsdog"
DEPLOY_DIR="/home/deploy"
STATE_DIR="/var/lib/botkit-rollout"
OVERRIDE_DIR="$STATE_DIR/overrides"
STATE_FILE="$STATE_DIR/state.txt"
LOG="/var/log/botkit-rollout.log"
# From fleet.env, not written here. The hardcoded form was the one thing that kept this
# script out of version control: ops/e2e/tests/test_fleet_contract.py fails any monitored
# script that names a monitoring address itself, and this file named 127.0.0.1:9093. The
# per-bot health and metrics URLs below stay literal on purpose - those are the bot's own
# port from the map above, not a monitoring endpoint.
AM_URL="$ALERTMANAGER_ALERTS_URL"
HEALTH_TRIES=6
HEALTH_WAIT=3

bot="${1:-}"
IMG="${2:-main}"
DRY=0
FAILPOINT=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --failpoint=*) FAILPOINT="${a#*=}" ;;
  esac
done

now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(now) $*" | tee -a "$LOG" >&2; }

if [[ -z "${PORTS[$bot]:-}" ]]; then
  echo "ERROR: unknown bot '$bot'. Valid: ${!PORTS[*]}" >&2
  exit 2
fi
PORT="${PORTS[$bot]}"
COMPOSE_DIR="$DEPLOY_DIR/botkit-$bot"
COMPOSE_FILE="$COMPOSE_DIR/deploy/compose.yml"
ENV_FILE="/usr/local/etc/botkit/$bot.env"
OVERRIDE_FILE="$OVERRIDE_DIR/$bot.yml"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: $COMPOSE_FILE not found" >&2
  exit 2
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found (secret env, root:root 600)" >&2
  exit 2
fi

SV=$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --services 2>/dev/null | head -1)
SV="${SV:-bot}"
TARGET_IMAGE="$REGISTRY/botkit-$bot:$IMG"
CUR_IMAGE=$(docker inspect -f '{{.Config.Image}}' "botkit-$bot" 2>/dev/null || echo "unknown")

echo "=== botkit rollout: $bot  port=$PORT"
echo "  current : $CUR_IMAGE"
echo "  target  : $TARGET_IMAGE"
echo "  service : $SV (compose $COMPOSE_FILE)"

if [[ "$DRY" == 1 ]]; then
  echo "  (--dry-run) plan: pull $TARGET_IMAGE -> override $OVERRIDE_FILE -> up -d --no-deps -> health x$HEALTH_TRIES on 127.0.0.1:$PORT -> metrics>=28 -> rollback to $CUR_IMAGE on failure"
  exit 0
fi

mkdir -p "$STATE_DIR" "$OVERRIDE_DIR"
log "=== rollout start bot=$bot target=$TARGET_IMAGE cur=$CUR_IMAGE"

if [[ "$TARGET_IMAGE" == "$CUR_IMAGE" && -z "$FAILPOINT" ]]; then
  log "bot=$bot already on $TARGET_IMAGE (idempotent skip)"
  echo "  -> already on target image, nothing to do"
  exit 0
fi

health_check() {
  local tries=0 code
  while [[ $tries -lt $HEALTH_TRIES ]]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/health" || echo 000)
    if [[ "$code" == "200" ]]; then
      echo "ok"; return 0
    fi
    tries=$((tries + 1)); sleep "$HEALTH_WAIT"
  done
  echo "fail(code=$code)"; return 1
}

send_alert() {
  local sev="$1" reason="$2"
  if [[ -n "$FAILPOINT" ]]; then
    log "bot=$bot (failpoint, alert suppressed): $reason"
    return
  fi
  payload="[{\"labels\":{\"alertname\":\"BotkitRolloutFailed\",\"severity\":\"$sev\",\"bot\":\"$bot\",\"service\":\"botkit-rollout\"},\"annotations\":{\"summary\":\"rollout fail $bot\",\"description\":\"${reason:0:200}\"}}]"
  curl -s -X POST -d "$payload" -H "Content-Type: application/json" "$AM_URL" --max-time 5 >/dev/null 2>&1 \
    && log "bot=$bot alert sent ($sev)" || log "bot=$bot WARN alert send failed"
}

apply_image() {
  local image="$1"
  if [[ -n "$image" ]]; then
    cat > "$OVERRIDE_FILE" <<OVERRIDE
services:
  ${SV}:
    image: ${image}
OVERRIDE
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" -f "$OVERRIDE_FILE" up -d --no-deps
  else
    rm -f "$OVERRIDE_FILE"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --no-deps
  fi
}

log "bot=$bot pulling $TARGET_IMAGE ..."
if ! docker pull "$TARGET_IMAGE"; then
  log "bot=$bot WARN pull failed: $TARGET_IMAGE (container untouched)"
  echo "  -> pull failed, container untouched"; exit 3
fi
DIGEST=$(docker image inspect -f '{{index .RepoDigests 0}}' "$TARGET_IMAGE" 2>/dev/null || echo "n/a")
log "bot=$bot pulled digest=$DIGEST"

log "bot=$bot deploying $TARGET_IMAGE ..."
apply_image "$TARGET_IMAGE"

HEALTH_RESULT="ok"
if [[ "$FAILPOINT" == "health" ]]; then
  HEALTH_RESULT="fail"
  log "bot=$bot failpoint=health: simulated failure"
else
  log "bot=$bot waiting health 127.0.0.1:$PORT ..."
  HEALTH_RESULT=$(health_check)
  log "bot=$bot health=$HEALTH_RESULT"
fi

if [[ "$HEALTH_RESULT" == "ok" ]]; then
  if [[ "$FAILPOINT" == "health" ]]; then
    HEALTH_RESULT="fail"
  else
    METRICS=$(curl -s --max-time 3 "http://127.0.0.1:$PORT/metrics" | grep -c '^botkit_' || echo 0)
    if [[ "$METRICS" -lt 28 ]]; then
      log "bot=$bot WARN metrics only $METRICS botkit_ lines (soft gate)"
    else
      log "bot=$bot metrics=$METRICS lines OK"
    fi
  fi
fi

if [[ "$HEALTH_RESULT" != "ok" ]]; then
  log "bot=$bot HEALTH FAIL -> rollback to $CUR_IMAGE"
  if ! apply_image "$CUR_IMAGE"; then
    log "bot=$bot CRITICAL rollback compose step failed"
    send_alert critical "rollback failed for $bot (image $CUR_IMAGE)"
    exit 4
  fi
  sleep "$HEALTH_WAIT"
  RB=$(health_check)
  log "bot=$bot after-rollback health=$RB"
  if [[ "$RB" == "ok" ]]; then
    send_alert warning "rollback OK: $bot reverted to $CUR_IMAGE"
    echo "  -> ROLLBACK done, health restored ($CUR_IMAGE)"
    printf '%s|%s|%s|%s\n' "$bot" "$CUR_IMAGE" "$DIGEST" "$(now)" >> "$STATE_FILE"
    exit 0
  fi
  send_alert critical "bot DOWN after rollback: $bot ($CUR_IMAGE)"
  echo "  -> CRITICAL: bot down after rollback"; exit 5
fi

printf '%s|%s|%s|%s\n' "$bot" "$TARGET_IMAGE" "$DIGEST" "$(now)" >> "$STATE_FILE"
log "bot=$bot rollout OK -> $TARGET_IMAGE"
echo "  -> OK: $bot now on $TARGET_IMAGE (digest $DIGEST)"