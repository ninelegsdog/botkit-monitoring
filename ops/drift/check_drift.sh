#!/usr/bin/env bash
# check_drift.sh — сверка прод-деплоя 9 ботов с каноном (HEAD, image-tag sha,
# network, health, compose-валидация). Расхождение -> алерт BotkitDrift.
# Запускается systemd timer (каждые 15 мин) на ПРОДЕ как root.
#
# Семантика:
#   - tracking-mismatch (HEAD != origin/main ИЛИ image-tag-sha != HEAD-sha):
#     это окно rollout после пуша (auto-rollout ~15 мин). Алерт — только если
#     mismatch держится > GRACE_S (30 мин) — настоящий застрявший дрейф.
#   - критично (host-network, контейнер down, compose fail, /health не 200):
#     алерт сразу.
# Лог: /var/log/botkit-drift.log; троттлинг алертов 6ч на бота.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPLOY_ROOT=/home/deploy
ENV_ROOT=/usr/local/etc/botkit
VALIDATOR=/opt/botkit-drift/validate_compose.py
CHANNEL=""
CONF=/opt/botkit-rollout/version.conf
[ -f "$CONF" ] && source "$CONF"
CHANNEL="${CHANNEL:-v0.8.2}"

STATE_DIR=/var/backups/botkit-drift
WATCH_DIR="$STATE_DIR/watch"
ALERTED_DIR="$STATE_DIR/alerted"
LOG=/var/log/botkit-drift.log
AM_URL="http://127.0.0.1:9093/api/v2/alerts"
THROTTLE=21600   # 6h между повторами алерта
GRACE_S=1800     # 30 мин окно rollout для tracking-mismatch

# Флот — из ops/lib/fleet.env (единый источник истины); путь overridable для прод-копии.
FLEET_ENV="${BOTKIT_FLEET_ENV:-$HERE/../lib/fleet.env}"
[ -f "$FLEET_ENV" ] || FLEET_ENV=/root/botkit-webhook-check/fleet.env
if [ -f "$FLEET_ENV" ]; then
  # shellcheck disable=SC1090
  . "$FLEET_ENV"
fi
BOTS="${FLEET:-bookingbot:8081 leadgen:8082 store:8083 support:8084 membership:8085 pricesentry:8086 docuflow:8087 delivery:8088 reminder:8089}"

mkdir -p "$STATE_DIR" "$WATCH_DIR" "$ALERTED_DIR"
now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
now_s() { date +%s; }
log() { echo "$(now) $*" >> "$LOG"; }

send_alert() { # $1=b $2=sev $3=reason
  local bot="$1" sev="$2" reason="$3" last="$ALERTED_DIR/$bot" age
  if [ -f "$last" ]; then
    age=$(( $(now_s) - $(stat -c %Y "$last") ))
    [ "$age" -lt "$THROTTLE" ] && { log "ALERT throttled $bot ($reason)"; return 0; }
  fi
  local payload
  payload="[{\"labels\":{\"alertname\":\"BotkitDrift\",\"severity\":\"$sev\",\"bot\":\"$bot\",\"service\":\"botkit-drift\"},\"annotations\":{\"summary\":\"drift $bot\",\"description\":\"${reason:0:200}\"}}]"
  curl -s -o /dev/null -X POST -d "$payload" -H "Content-Type: application/json" "$AM_URL" --max-time 5 \
    && { echo "$(now)" > "$last"; log "ALERT sent ($sev $bot): $reason"; } \
    || log "ALERT send FAILED ($bot): $reason"
}

rc_total=0
for entry in $BOTS; do
  bot="${entry%%:*}"
  port="${entry##*:}"
  d="$DEPLOY_ROOT/botkit-$bot"
  ctr="botkit-$bot"
  [ -d "$d" ] || { send_alert "$bot" critical "deploy dir missing: $d"; rc_total=1; continue; }
  critical=()
  track=()

  # --- HEAD vs origin/main (tracking) ---
  if ! git -c safe.directory="$d" -C "$d" fetch --quiet origin main 2>>"$LOG"; then
    track+=("git fetch failed")
  else
    head=$(git -c safe.directory="$d" -C "$d" rev-parse HEAD 2>/dev/null)
    origin=$(git -c safe.directory="$d" -C "$d" rev-parse origin/main 2>/dev/null)
    if [ -n "$head" ] && [ -n "$origin" ] && [ "$head" != "$origin" ]; then
      track+=("HEAD ${head:0:7} != origin/main ${origin:0:7}")
    elif [ -z "$origin" ]; then
      track+=("origin/main unresolvable")
    fi
  fi

  # --- network: bridge botkit_<b>, not host (CRITICAL) ---
  netjson=$(docker inspect "$ctr" -f '{{json .NetworkSettings.Networks}}' 2>/dev/null || echo "")
  if [ -z "$netjson" ]; then
    critical+=("container inspect failed")
  else
    if ! echo "$netjson" | grep -q "botkit_$bot"; then
      critical+=("NOT on botkit_$bot network")
    fi
    if [ "${netjson%%\:*}x" != "x" ] && echo "$netjson" | grep -q '"host"'; then
      critical+=("HOST network mode")
    fi
  fi

  # --- container running (CRITICAL) ---
  running=$(docker inspect -f '{{.State.Running}}' "$ctr" 2>/dev/null || echo "unknown")
  [ "$running" = "true" ] || critical+=("container not running ($running)")

  # --- image tag: channel prefix + sha matches HEAD (tracking) ---
  img=$(docker inspect -f '{{.Config.Image}}' "$ctr" 2>/dev/null || echo "unknown")
  case "$img" in
    *":$CHANNEL-"*) ;;
    *) track+=("image tag not on channel $CHANNEL: $img") ;;
  esac
  if [ -n "${head:-}" ] && [ "$img" != "unknown" ]; then
    imgsha=$(echo "$img" | sed -n "s#.*/$CHANNEL-\([0-9a-f]\{7\}\).*#\1#p")
    if [ -n "$imgsha" ] && [ "$imgsha" != "${head:0:7}" ]; then
      track+=("image tag sha $imgsha != HEAD sha ${head:0:7}")
    fi
  fi

  # --- /health (CRITICAL) ---
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$port/health" || echo 000)
  [ "$code" = "200" ] || critical+=("/health=$code")

  # --- compose validation (CRITICAL) ---
  if [ -f "$VALIDATOR" ] && [ -f "$d/deploy/compose.yml" ]; then
    if ! python3 "$VALIDATOR" "$d/deploy/compose.yml" --bot="$bot" >/dev/null 2>>"$LOG"; then
      critical+=("compose validation FAILED")
    fi
    if ! docker compose --env-file "$ENV_ROOT/$bot.env" -f "$d/deploy/compose.yml" config --quiet 2>>"$LOG"; then
      critical+=("docker compose config failed")
    fi
  else
    critical+=("validator/compose missing")
  fi

  # --- emit ---
  if [ ${#critical[@]} -gt 0 ]; then
    log "CRITICAL $bot: ${critical[*]} (img=$img)"
    send_alert "$bot" critical "${critical[*]}"
    rc_total=1
    continue
  fi

  if [ ${#track[@]} -gt 0 ]; then
    # grace window: track first observation; alert only if persists > GRACE_S
    wf="$WATCH_DIR/$bot"
    if [ ! -f "$wf" ]; then
      echo "$(now_s)" > "$wf"
      log "WATCH start $bot: ${track[*]}"
    else
      t0=$(cat "$wf")
      age=$(( $(now_s) - t0 ))
      if [ "$age" -gt "$GRACE_S" ]; then
        log "DRIFT $bot (${age}s > grace): ${track[*]} (img=$img head=${head:-na})"
        send_alert "$bot" warning "${track[*]}"
        rc_total=1
      else
        log "WATCH hold $bot (${age}s < grace): ${track[*]}"
      fi
    fi
  else
    rm -f "$WATCH_DIR/$bot"
    log "OK $bot (head=${head:-na} img=$img health=200)"
  fi
done

exit "$rc_total"