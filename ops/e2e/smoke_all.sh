#!/usr/bin/env bash
#
# smoke_all.sh — полный путь деплоя: публичный webhook (с секретом) ->
# nginx -> бот -> лог aiogram; + negative-auth (401 без токена);
# + /health; + статус контейнера. Запускать на ПРОДЕ.
# Exit: 0 если все проверки зелёные, 1 иначе.
#
set -u

BASE="http://127.0.0.1"
DOMAIN="https://ninelegsbots.duckdns.org/webhook"
BOTS="bookingbot:8081 leadgen:8082 store:8083 support:8084 membership:8085 pricesentry:8086 docuflow:8087 delivery:8088 reminder:8089"

FAIL=0
echo "bot | auth200 | noauth401 | health200 | loghit | up"
for entry in $BOTS; do
  name="${entry%%:*}"
  port="${entry##*:}"
  ctr="botkit-$name"

  # сеcret: из контейнера (fallback: .env на хосте)
  sec=$(docker exec "$ctr" printenv TELEGRAM_WEBHOOK_SECRET 2>/dev/null | tr -d '\r')
  if [ -z "$sec" ]; then
    sec=$(grep -E '^TELEGRAM_WEBHOOK_SECRET=' "/home/deploy/$name/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r')
  fi
  if [ -z "$sec" ]; then
    echo "$name | MISSING-SECRET"; FAIL=1; continue
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

  if docker ps --format '{{.Names}}' | grep -qx "$ctr"; then up="up"; else up="DOWN"; FAIL=1; fi

  [ "$auth" = "200" ] || FAIL=1
  [ "$noauth" = "401" ] || FAIL=1
  [ "$health" = "200" ] || FAIL=1
  [ "$loghit" -gt 0 ] || FAIL=1

  echo "$name | $auth | $noauth | $health | $loghit | $up"
done

[ "$FAIL" = "0" ] && echo "SMOKE_ALL: PASS" || echo "SMOKE_ALL: FAIL"
exit "$FAIL"