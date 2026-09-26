#!/usr/bin/env bash
#
# smoke_all.sh — полный путь деплоя: публичный webhook (с секретом) ->
# nginx -> бот -> лог aiogram; + negative-auth (401 без токена);
# + /health; + статус контейнера. Запускать на ПРОДЕ.
# Exit: 0 если все проверки зелёные, 1 иначе.
# Алерт BotkitSmokeFailed в Alertmanager при любом FAIL (троттлинг 1ч на бота).
# Лог: /var/log/botkit-smoke.log
#
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BASE="http://127.0.0.1"
DOMAIN="https://${WEBHOOK_DOMAIN:-ninelegsbots.duckdns.org}/webhook"
# Флот — из ops/lib/fleet.env (единый источник истины); путь overridable для прод-копии.
FLEET_ENV="${BOTKIT_FLEET_ENV:-$HERE/../lib/fleet.env}"
[ -f "$FLEET_ENV" ] || FLEET_ENV=/root/botkit-webhook-check/fleet.env
if [ -f "$FLEET_ENV" ]; then
  # shellcheck disable=SC1090
  . "$FLEET_ENV"
fi
BOTS="${FLEET:-bookingbot:8081 leadgen:8082 store:8083 support:8084 membership:8085 pricesentry:8086 docuflow:8087 delivery:8088 reminder:8089}"

LOG=/var/log/botkit-smoke.log
AM_URL="http://127.0.0.1:9093/api/v2/alerts"
ALERTED_DIR=/var/backups/botkit-smoke/alerted
THROTTLE=3600   # 1h между повторными алертами на бота
mkdir -p "$ALERTED_DIR"

now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(now) $*" >> "$LOG"; }

send_alert() { # $1=bot $2=reason
  local bot reason last age
  bot="$1"; reason="$2"
  last="$ALERTED_DIR/$bot"
  if [ -f "$last" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$last") ))
    [ "$age" -lt "$THROTTLE" ] && { log "ALERT throttled $bot ($reason)"; return 0; }
  fi
  local payload
  payload="[{\"labels\":{\"alertname\":\"BotkitSmokeFailed\",\"severity\":\"critical\",\"bot\":\"$bot\",\"service\":\"botkit-smoke\"},\"annotations\":{\"summary\":\"smoke fail $bot\",\"description\":\"${reason:0:200}\"}}]"
  curl -s -o /dev/null -X POST -d "$payload" -H "Content-Type: application/json" "$AM_URL" --max-time 5 \
    && { echo "$(now)" > "$last"; log "ALERT sent ($bot): $reason"; } \
    || log "ALERT send FAILED ($bot): $reason"
}

FAIL=0
log "SMOKE start"
echo "bot | auth200 | noauth401 | health200 | loghit | up"
for entry in $BOTS; do
  name="${entry%%:*}"
  port="${entry##*:}"
  ctr="botkit-$name"
  bot_fail=0

  # сеcret: из контейнера (fallback: /usr/local/etc/botkit env)
  sec=$(docker exec "$ctr" printenv TELEGRAM_WEBHOOK_SECRET 2>/dev/null | tr -d '\r')
  if [ -z "$sec" ]; then
    sec=$(grep -E '^TELEGRAM_WEBHOOK_SECRET=' "/usr/local/etc/botkit/$name.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r')
  fi
  if [ -z "$sec" ]; then
    echo "$name | MISSING-SECRET"; log "FAIL $name MISSING-SECRET"
    send_alert "$name" "webhook secret missing"
    FAIL=1; continue
  fi

  uid=$(( $(date +%s%N) ))

  auth=$(curl -k -s -o /dev/null -w '%{http_code}' --max-time 8 -X POST "$DOMAIN/$name" \
    -H "X-Telegram-Bot-Api-Secret-Token: $sec" \
    -H "Content-Type: application/json" \
    -d "{\"update_id\":$uid}")

  noauth=$(curl -k -s -o /dev/null -w '%{http_code}' --max-time 8 -X POST "$DOMAIN/$name" \
    -H "Content-Type: application/json" -d "{\"update_id\":$uid}")

  health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$BASE:$port/health")

  sleep 2
  loghit=$(docker logs --since 15s "$ctr" 2>&1 | grep -c "$uid")

  if docker ps --format '{{.Names}}' | grep -qx "$ctr"; then up="up"; else up="DOWN"; bot_fail=1; fi

  [ "$auth" = "200" ] || bot_fail=1
  [ "$noauth" = "401" ] || bot_fail=1
  [ "$health" = "200" ] || bot_fail=1
  [ "$loghit" -gt 0 ] || bot_fail=1

  echo "$name | $auth | $noauth | $health | $loghit | $up"
  if [ "$bot_fail" -ne 0 ]; then
    reason="auth=$auth noauth=$noauth health=$health loghit=$loghit up=$up"
    log "FAIL $name $reason"
    send_alert "$name" "$reason"
    FAIL=1
  else
    log "OK $name"
  fi
done

if [ "$FAIL" = "0" ]; then
  echo "SMOKE_ALL: PASS"; log "SMOKE_ALL: PASS"
else
  echo "SMOKE_ALL: FAIL"; log "SMOKE_ALL: FAIL"
fi
exit "$FAIL"