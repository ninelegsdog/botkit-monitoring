#!/usr/bin/env bash
#
# smoke_all.sh — полный путь деплоя: публичный webhook (с секретом) ->
# nginx -> бот -> лог aiogram; + negative-auth (401 без токена);
# + /health; + статус контейнера; + проверка TLS с реальной верификацией.
# Запускать на ПРОДЕ.
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

LOG="${BOTKIT_SMOKE_LOG:-/var/log/botkit-smoke.log}"
AM_URL="http://127.0.0.1:9093/api/v2/alerts"
ALERTED_DIR="${BOTKIT_SMOKE_ALERTED_DIR:-/var/backups/botkit-smoke/alerted}"
THROTTLE=3600   # 1h между повторными алертами на бота
mkdir -p "$ALERTED_DIR"

# An unwritable log is not a cosmetic problem: the run would report PASS with no
# evidence, and the alert throttle markers would silently never appear, so every run
# would re-alert. Refuse up front instead of losing the audit trail for 9 bots of work.
for sink in "$LOG" "$ALERTED_DIR"; do
  dir=$(dirname "$sink")
  [ -d "$dir" ] || { mkdir -p "$dir" 2>/dev/null || { echo "FATAL: cannot create $dir (run as root, or set BOTKIT_SMOKE_LOG/BOTKIT_SMOKE_ALERTED_DIR)" >&2; exit 2; }; }
  [ -w "$dir" ] || { echo "FATAL: $dir is not writable by $(id -un) (run as root, or set BOTKIT_SMOKE_LOG/BOTKIT_SMOKE_ALERTED_DIR)" >&2; exit 2; }
  [ -e "$sink" ] && [ ! -w "$sink" ] && { echo "FATAL: $sink is not writable by $(id -un) (run as root, or set BOTKIT_SMOKE_LOG/BOTKIT_SMOKE_ALERTED_DIR)" >&2; exit 2; }
done
: >> "$LOG" || { echo "FATAL: cannot append to $LOG" >&2; exit 2; }

now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(now) $*" >> "$LOG"; }

# Public webhook calls verify TLS: no -k. A check that skips verification reports a
# green "external TLS ok" for a certificate no third party would accept, which is how
# 11.09-26.09 stayed invisible. curl distinguishes the failure in its exit code, so
# keep it: rc 60 is an untrusted chain, 51 a name mismatch, and both must be readable in
# the smoke output instead of collapsing into a bare http_code=000.
HTTP_CODE=000
CURL_RC=0
CURL_ERR=""
post_public() { # $1=url, rest: curl args -> HTTP_CODE, CURL_RC, CURL_ERR
  local url="$1" err rc
  shift
  err=$(mktemp)
  HTTP_CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$@" "$url" 2>"$err")
  rc=$?
  CURL_RC=$rc
  CURL_ERR=$(tr '\n' ' ' <"$err" | cut -c1-120)
  rm -f "$err"
}

tls_state() { # CURL_RC/HTTP_CODE -> one word for the report
  if [ "$CURL_RC" = "0" ]; then echo verified
  elif [ "$CURL_RC" = "60" ]; then echo TLS_UNTRUSTED
  elif [ "$CURL_RC" = "51" ]; then echo TLS_NAME_MISMATCH
  elif [ "$CURL_RC" = "6" ] || [ "$CURL_RC" = "7" ]; then echo TLS_UNREACHABLE
  elif [ "$CURL_RC" = "28" ]; then echo TIMEOUT
  else echo "rc=$CURL_RC"; fi
}

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
echo "bot | tls | auth200 | noauth401 | health200 | loghit | up"
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
    echo "$name | SKIP | - | - | - | - | MISSING-SECRET"; log "FAIL $name MISSING-SECRET"
    send_alert "$name" "webhook secret missing"
    FAIL=1; continue
  fi

  uid=$(( $(date +%s%N) ))

  post_public "$DOMAIN/$name" -X POST \
    -H "X-Telegram-Bot-Api-Secret-Token: $sec" \
    -H "Content-Type: application/json" \
    -d "{\"update_id\":$uid}"
  auth="$HTTP_CODE"
  auth_rc="$CURL_RC"
  auth_err="$CURL_ERR"
  tls="$(tls_state)"

  post_public "$DOMAIN/$name" -X POST \
    -H "Content-Type: application/json" -d "{\"update_id\":$uid}"
  noauth="$HTTP_CODE"

  health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "$BASE:$port/health")

  sleep 2
  loghit=$(docker logs --since 15s "$ctr" 2>&1 | grep -c "$uid")

  if docker ps --format '{{.Names}}' | grep -qx "$ctr"; then up="up"; else up="DOWN"; bot_fail=1; fi

  [ "$auth" = "200" ] || bot_fail=1
  [ "$tls" = "verified" ] || bot_fail=1
  [ "$noauth" = "401" ] || bot_fail=1
  [ "$health" = "200" ] || bot_fail=1
  [ "$loghit" -gt 0 ] || bot_fail=1

  echo "$name | $tls | $auth | $noauth | $health | $loghit | $up"
  if [ "$bot_fail" -ne 0 ]; then
    reason="tls=$tls auth=$auth(rc=$auth_rc) noauth=$noauth health=$health loghit=$loghit up=$up"
    [ -n "$auth_err" ] && reason="$reason err=$auth_err"
    log "FAIL $name $reason"
    send_alert "$name" "$reason"
    FAIL=1
  else
    log "OK $name tls=$tls auth=$auth noauth=$noauth health=$health"
  fi
done

if [ "$FAIL" = "0" ]; then
  echo "SMOKE_ALL: PASS"; log "SMOKE_ALL: PASS"
else
  echo "SMOKE_ALL: FAIL"; log "SMOKE_ALL: FAIL"
fi
exit "$FAIL"