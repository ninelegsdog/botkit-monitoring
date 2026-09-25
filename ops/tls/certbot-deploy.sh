#!/usr/bin/env bash
set -euo pipefail

DOMAIN=ninelegsbots.duckdns.org
SRC=/home/deploy/reverse-proxy/letsencrypt/live/$DOMAIN
DST=/home/deploy/reverse-proxy/certs
BACKUP_TAG=$(date -u +%Y%m%d%H%M%S)
BOTS="bookingbot leadgen store support membership pricesentry docuflow delivery reminder"

[ -s "$SRC/fullchain.pem" ]
[ -s "$SRC/privkey.pem" ]
openssl x509 -in "$SRC/fullchain.pem" -noout -checkend 86400 >/dev/null
cert_pub=$(openssl x509 -in "$SRC/fullchain.pem" -pubkey -noout | openssl pkey -pubin -outform der | sha256sum | cut -d" " -f1)
key_pub=$(openssl pkey -in "$SRC/privkey.pem" -pubout -outform der | sha256sum | cut -d" " -f1)
[ "$cert_pub" = "$key_pub" ]

install -m 0600 "$DST/fullchain.pem" "$DST/fullchain.pem.pre-$BACKUP_TAG" 2>/dev/null || true
install -m 0600 "$DST/privkey.pem" "$DST/privkey.pem.pre-$BACKUP_TAG" 2>/dev/null || true
install -m 0644 "$SRC/fullchain.pem" "$DST/fullchain.pem"
install -m 0640 "$SRC/privkey.pem" "$DST/privkey.pem"

if ! docker exec reverse-proxy-nginx-1 nginx -t; then
  [ -s "$DST/fullchain.pem.pre-$BACKUP_TAG" ] && install -m 0644 "$DST/fullchain.pem.pre-$BACKUP_TAG" "$DST/fullchain.pem"
  [ -s "$DST/privkey.pem.pre-$BACKUP_TAG" ] && install -m 0640 "$DST/privkey.pem.pre-$BACKUP_TAG" "$DST/privkey.pem"
  docker exec reverse-proxy-nginx-1 nginx -s reload || true
  exit 1
fi
docker exec reverse-proxy-nginx-1 nginx -s reload

for bot in $BOTS; do
  compose_dir=/home/deploy/botkit-$bot
  override=/var/lib/botkit-rollout/overrides/$bot.yml
  [ -d "$compose_dir" ]
  [ -f "$override" ]
  cd "$compose_dir"
  docker compose --env-file /usr/local/etc/botkit/$bot.env -f deploy/compose.yml -f "$override" up -d --force-recreate --no-deps >/dev/null
  ready=0
  for _ in $(seq 1 45); do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' botkit-$bot 2>/dev/null || true)" = healthy ]; then
      ready=1
      break
    fi
    sleep 2
  done
  [ "$ready" = 1 ] || { echo "certbot-deploy: $bot failed health" >&2; exit 1; }
done
