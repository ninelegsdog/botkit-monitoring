#!/usr/bin/env bash
# deploy_rollout.sh — health-gated single-bot rollout with automatic rollback
# Usage: deploy_rollout.sh <bot> [image:tag] [--dry-run] [--failpoint=health]
#   <bot>            bookingbot|delivery|docuflow|leadgen|membership|pricesentry|reminder|store|support
#   image:tag        REQUIRED. e.g. "v0.8.2-abc123". There is no default on purpose: this
#                    used to fall back to "main", a moving tag with no health gate behind
#                    it, so forgetting the argument deployed whatever main pointed at. If
#                    you really want main, pass main - explicitly, where it is a choice.
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

# Flags are parsed by shape, not by position. The original took IMG="${2:-main}" blindly,
# so `--failpoint=health` - the drill documented in this file's own header - landed in the
# image tag: TARGET_IMAGE became <registry>/botkit-reminder:--failpoint=health, docker pull
# refused, and the rollback drill could never run. Proven live on 03.10, where the plan line
# printed the flag as the tag. `--dry-run` had the same defect and merely hid it by exiting
# early.
bot=""
IMG=""
DRY=0
FAILPOINT=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --failpoint=*) FAILPOINT="${a#*=}" ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $a" >&2; exit 2 ;;
    *)
      if [ -z "$bot" ]; then
        bot="$a"
      elif [ -z "$IMG" ]; then
        IMG="$a"
      else
        echo "unexpected argument: $a" >&2
        exit 2
      fi
      ;;
  esac
done
if [ -z "$bot" ]; then
  echo "usage: $0 <bot> <image:tag> [--dry-run] [--failpoint=health]" >&2
  exit 2
fi
if [[ -z "$IMG" ]]; then
  echo "usage: $0 <bot> <image:tag> [--dry-run] [--failpoint=health]" >&2
  echo "note: image:tag is required. There is no default - a rollout to a moving tag is" >&2
  echo "      not a rollout. check_updates.sh always passes <channel>-<sha7>." >&2
  exit 2
fi

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

# The container must be able to open its SQLite file before we replace it.
# compose pins the runtime user (user: "1001:1001") and bind-mounts ../data into
# /app/data, while the host directory keeps whatever owner it happens to have, and
# nothing in the compose healthcheck reads SQLite - it probes /health and a Redis
# socket. So a mismatch is invisible: on 03.10 a fleet-wide `chown -R deploy:deploy`
# left all nine bots unable to open their database while every one of them still
# reported healthy, and they only broke when a rollout recreated a container. A
# long-lived process survives on the file descriptor it opened before the change, so
# the drift check reports OK right up until the moment a deploy makes it permanent.
# The fix belongs here, before the container is touched: refusing is free, an
# outage is not.
set_image_tag() {
  local tag="$1" tmp
  # The tag is interpolated into sed and into a KEY=VALUE line, so refuse anything that
  # could carry a newline or a sed metacharacter instead of quoting our way around it.
  if [[ ! "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9._:@-]*$ ]]; then
    log "bot=$bot REFUSING tag with unexpected characters: $tag"
    return 1
  fi
  tmp=$(mktemp "${ENV_FILE}.XXXXXX") || return 1
  chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
  if grep -q '^IMAGE_TAG=' "$ENV_FILE"; then
    sed "s|^IMAGE_TAG=.*|IMAGE_TAG=$tag|" "$ENV_FILE" >"$tmp" || { rm -f "$tmp"; return 1; }
  else
    { cat "$ENV_FILE"; printf 'IMAGE_TAG=%s\n' "$tag"; } >"$tmp" || { rm -f "$tmp"; return 1; }
  fi
  # Copy back rather than mv: mv from a temp file replaces the inode, and the env file's
  # mode and owner are load-bearing - it holds REDIS credentials and is read with --env-file.
  cat "$tmp" >"$ENV_FILE" || { rm -f "$tmp"; return 1; }
  rm -f "$tmp"
}

read_image_tag() {
  sed -n 's/^IMAGE_TAG=//p' "$ENV_FILE" 2>/dev/null | tail -1
}

compose_up() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --no-deps
}

# Both bind-mounts compose gives the container. data/ holds the database the app opens;
# backups/ is a legacy mount - since 05.10 the consistent snapshot goes into the
# container's tmpfs and is streamed out with exec cat (ops/lib/snapshot.sh), and the
# bots' own code never mentions the directory. It is probed anyway because both
# directories are still owned by whoever last touched them, and ownership drift there
# is what hid for two days: on 05.10 data/ was writable while backups/ was still
# root:root 700, which broke every consistent export while a data/-only preflight
# passed. Every message below therefore starts with the mount name, so the refusal can
# name the fix.
data_preflight() {
  local probe mount dir duid cuid
  for probe in /app/data /app/backups; do
    mount=${probe#/app/}
    dir="$COMPOSE_DIR/$mount"
    if [[ ! -d "$dir" ]]; then
      echo "$mount: missing $dir"; return 1
    fi
    duid=$(stat -c %u "$dir" 2>/dev/null || echo "?")
    cuid=$(docker inspect -f '{{.Config.User}}' "botkit-$bot" 2>/dev/null || echo "?")
    if [[ "$(docker inspect -f '{{.State.Running}}' "botkit-$bot" 2>/dev/null)" != "true" ]]; then
      # Nothing to exec into. Fall back to the ownership comparison so a stopped bot
      # is not waved through on a check that never ran.
      if [[ "$duid" != "${cuid%%:*}" ]]; then
        echo "$mount: container down and dir uid=$duid != container user=$cuid"; return 1
      fi
      continue
    fi
    if ! docker exec "botkit-$bot" sh -c "touch $probe/.rollout-preflight && rm -f $probe/.rollout-preflight" 2>/dev/null; then
      echo "$mount: not writable by container user (dir uid=$duid, container user=$cuid)"
      return 1
    fi
  done
  echo "ok(both mounts writable, container user=$cuid)"; return 0
}

SV=$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --services 2>/dev/null | head -1)
SV="${SV:-bot}"
TARGET_IMAGE="$REGISTRY/botkit-$bot:$IMG"
CUR_IMAGE=$(docker inspect -f '{{.Config.Image}}' "botkit-$bot" 2>/dev/null || echo "unknown")

echo "=== botkit rollout: $bot  port=$PORT"
echo "  current : $CUR_IMAGE"
echo "  target  : $TARGET_IMAGE"
echo "  service : $SV (compose $COMPOSE_FILE)"

if [[ "$DRY" == 1 ]]; then
  echo "  (--dry-run) preflight: data/ and backups/ writable by container user -> $(data_preflight || echo FAILED)"
  echo "  (--dry-run) image pin: IMAGE_TAG=$IMG -> $ENV_FILE (currently IMAGE_TAG=$(read_image_tag || echo none))"
  echo "  (--dry-run) plan: pull $TARGET_IMAGE -> IMAGE_TAG=$IMG in env -> up -d --no-deps -> health x$HEALTH_TRIES on 127.0.0.1:$PORT -> metrics>=28 -> rollback to $CUR_IMAGE on failure"
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

# The image lives in exactly one place: IMAGE_TAG in the bot's env file, which compose
# already reads through --env-file. It used to live in a second place - a generated
# override in $OVERRIDE_DIR - and the two disagreed. Three things followed from that, all
# observed on the live fleet on 03.10:
#
#   * a manual `docker compose up -d` resolved IMAGE_TAG to nothing and fell back to
#     `:main`, silently replacing a health-gated pinned deployment with a moving tag;
#   * the override was never removed on the rollback path, so it outlived the rollout and
#     leaked into the next `compose up`;
#   * restore_bot.sh appends that override when it exists, and a stray brace in the array
#     it builds turned the filename into `over.yml}` - so a leftover override did not just
#     mislead, it broke the restore path.
#
# Two sources of truth is the defect, not the override itself. The env file is the one
# compose reads anyway, it survives a reboot, and it is the file an operator's command
# already points at. A tag that is only in a generated file is a tag nobody can see.



# Refuse before the container is touched. Rolling forward onto a data directory the
  # runtime user cannot write produces a container that answers /health 200 and cannot
  # open its database - the exact state the 03.10 drill walked into. A stopped rollout
  # costs one timer cycle; the alternative costs the bot.
  if ! PF=$(data_preflight); then
    log "bot=$bot DATA PREFLIGHT FAILED: $PF"
    send_alert critical "data preflight failed for $bot ($PF) - rollout refused, container untouched"
    echo "  -> REFUSED: $PF"
    # Every preflight message is prefixed with the mount name, so the refusal
    # names the directory that is actually wrong instead of always data/.
    echo "  -> fix: chown -R 1001:1001 $COMPOSE_DIR/${PF%%:*}"
    exit 6
  fi
  log "bot=$bot preflight ok ($PF)"

  log "bot=$bot pulling $TARGET_IMAGE ..."
if ! docker pull "$TARGET_IMAGE"; then
  log "bot=$bot WARN pull failed: $TARGET_IMAGE (container untouched)"
  echo "  -> pull failed, container untouched"; exit 3
fi
DIGEST=$(docker image inspect -f '{{index .RepoDigests 0}}' "$TARGET_IMAGE" 2>/dev/null || echo "n/a")
log "bot=$bot pulled digest=$DIGEST"

log "bot=$bot deploying $TARGET_IMAGE ..."
# PREV_TAG is read before anything is written, so the rollback restores the state the bot
# was actually in rather than a value guessed from the container's image reference - which
# breaks on a digest-pinned deployment, where there is no tag to parse out of it.
PREV_TAG=$(read_image_tag)
if [[ -z "$PREV_TAG" ]]; then
  # First rollout after the move off overrides: derive the tag from what is running so a
  # rollback has something to go back to.
  PREV_TAG="${CUR_IMAGE##*:}"
  [[ "$PREV_TAG" != "$CUR_IMAGE" ]] || PREV_TAG=""
fi
if ! set_image_tag "$IMG"; then
  log "bot=$bot CRITICAL could not write IMAGE_TAG=$IMG into $ENV_FILE"
  send_alert critical "cannot pin IMAGE_TAG for $bot in $ENV_FILE - container untouched"
  echo "  -> REFUSED: env file not writable, container untouched"
  exit 7
fi
if [[ -f "$OVERRIDE_FILE" ]]; then
  # Left over from a rollout that predates the env-file pin. It would silently win over
  # IMAGE_TAG on any compose run that includes it, including restore_bot.sh's.
  rm -f "$OVERRIDE_FILE" && log "bot=$bot removed stale override $OVERRIDE_FILE"
fi
compose_up

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
  log "bot=$bot HEALTH FAIL -> rollback to tag ${PREV_TAG:-<none>}"
  if [[ -z "$PREV_TAG" ]]; then
    log "bot=$bot CRITICAL no previous IMAGE_TAG to roll back to"
    send_alert critical "rollback impossible for $bot - no IMAGE_TAG recorded, image $CUR_IMAGE still running"
    echo "  -> CRITICAL: cannot roll back, no previous tag recorded"; exit 4
  fi
  if ! set_image_tag "$PREV_TAG" || ! compose_up; then
    log "bot=$bot CRITICAL rollback compose step failed"
    send_alert critical "rollback failed for $bot (tag $PREV_TAG)"
    exit 4
  fi
  sleep "$HEALTH_WAIT"
  RB=$(health_check)
  log "bot=$bot after-rollback health=$RB"
  if [[ "$RB" == "ok" ]]; then
    send_alert warning "rollback OK: $bot reverted to $PREV_TAG"
    echo "  -> ROLLBACK done, health restored ($PREV_TAG)"
    printf '%s|%s|%s|%s\n' "$bot" "$REGISTRY/botkit-$bot:$PREV_TAG" "$DIGEST" "$(now)" >> "$STATE_FILE"
    exit 0
  fi
  send_alert critical "bot DOWN after rollback: $bot ($PREV_TAG)"
  echo "  -> CRITICAL: bot down after rollback"; exit 5
fi

printf '%s|%s|%s|%s\n' "$bot" "$TARGET_IMAGE" "$DIGEST" "$(now)" >> "$STATE_FILE"
log "bot=$bot rollout OK -> $TARGET_IMAGE (IMAGE_TAG pinned in $ENV_FILE)"
echo "  -> OK: $bot now on $TARGET_IMAGE (digest $DIGEST)"